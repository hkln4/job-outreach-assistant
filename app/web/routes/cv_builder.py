"""CV builder UI (roadmap phase 5): the bullet bank, the tech-stack list, the
one-time template approval, and the per-application CV preview -> render ->
ATS check -> approve flow."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.cv_builder.assemble import assemble, base_cv, locked_sections
from app.cv_builder.bank import BULLETS_FOR_PRIORITY, approve_passing, can_approve, check_bullet, edit_bullet, generate_bullets, is_small, suggest_projects
from app.cv_builder.consistency import check_consistency
from app.cv_builder.render import RenderUnavailable
from app.cv_builder.service import approve, create_for_application, render_generated, update_selection
from app.cv_builder.skills import GROUP_TITLES, GROUPS, add_manual, evidence_status, import_cv_skills, rebuild_skills, selected_project_item_ids
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import CV, Application, Bullet, CVProject, GeneratedCV, OwnerSettings, ProfileItem, TechSkill
from app.web.deps import get_draft_llm
from app.web.errors import NotFoundError
from app.web.render import render

router = APIRouter(prefix="/cv-builder", tags=["cv-builder"])
templates = Jinja2Templates(directory="app/web/templates")


def _settings(db: Session) -> OwnerSettings:
    settings = db.query(OwnerSettings).first()
    if settings is None:
        settings = OwnerSettings()
        db.add(settings)
        db.commit()
    return settings


def _back(anchor: str = "") -> RedirectResponse:
    return RedirectResponse(url=f"/cv-builder{anchor}", status_code=303)


@router.get("")
def cv_builder_page(request: Request, error: str = "", db: Session = Depends(get_db)):
    """One page: projects + bullets and skills on the left, the CV itself on
    the right, re-rendered after every change."""
    if not db.query(CVProject).count():
        suggest_projects(db)  # first visit: start from suggestions, not an empty page
    selected_ids = selected_project_item_ids(db)
    skills = db.query(TechSkill).order_by(TechSkill.sort_order, TechSkill.id).all()
    projects = db.query(CVProject).order_by(CVProject.selected.desc(), CVProject.sort_order).all()
    bullets = [b for p in projects if p.selected for b in p.bullets]
    stale = [b for b in bullets if b.status == "draft" and "ats" not in (b.flags or {})]
    for bullet in stale:  # stored before the ATS rules existed
        bullet.flags = check_bullet(bullet.text, bullet.project.profile_item if bullet.project else None)
    if stale:
        db.commit()
    return render(
        templates,
        request,
        "cv_builder.html",
        {
            "projects": [p for p in projects if p.selected or not is_small(p)],
            "small_projects": [p for p in projects if not p.selected and is_small(p)],
            "skills_by_group": {g: [(s, evidence_status(s, selected_ids)) for s in skills if s.group == g] for g in GROUPS},
            "group_titles": GROUP_TITLES["en"],
            "groups": GROUPS,
            "cvs": db.query(CV).filter(CV.file_path != "").all(),
            "base_cv": base_cv(db),
            "locked": locked_sections(db),
            "settings": _settings(db),
            "consistency": check_consistency(db),
            "can_approve": can_approve,
            "bullets_for_priority": BULLETS_FOR_PRIORITY,
            "progress": {
                "projects": sum(1 for p in projects if p.selected),
                "approved": sum(1 for b in bullets if b.status == "approved"),
                "drafts": sum(1 for b in bullets if b.status == "draft"),
                "missing": sum(1 for p in projects if p.selected and not p.bullets),
            },
            "error": error,
        },
    )


@router.get("/preview.pdf")
def live_preview(drafts: int = 1, db: Session = Depends(get_db)):
    """The CV as it stands now (no posting), rendered on the fly for the
    preview pane - nothing stored."""
    generated = GeneratedCV(selection=assemble(db, include_drafts=bool(drafts)), language="en")
    try:
        from app.cv_builder.render import render as render_cv

        pdf, _ = render_cv(db, generated)
    except RenderUnavailable as exc:
        return Response(str(exc), status_code=503)
    return Response(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@router.post("/projects/suggest")
def projects_suggest(db: Session = Depends(get_db)):
    suggest_projects(db)
    return _back("#projects")


@router.post("/projects/{project_id}")
def project_update(
    project_id: int,
    action: str = Form("save"),
    subtitle: str = Form(""),
    link_url: str = Form(""),
    name: str = Form(""),
    priority: str = Form(""),
    db: Session = Depends(get_db),
    draft_llm: LLMProvider = Depends(get_draft_llm),
):
    project = db.get(CVProject, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")
    if action == "priority" and priority in BULLETS_FOR_PRIORITY:
        project.priority = priority
        project.owner_decided = True
    elif action == "approve_all":
        approve_passing(db, project)
    elif action == "toggle":
        project.selected = not project.selected
        project.owner_decided = True
    elif action == "bullets":
        try:
            generate_bullets(db, draft_llm, project)
        except ValueError as exc:
            return RedirectResponse(url=f"/cv-builder?error={str(exc)[:200]}#project-{project_id}", status_code=303)
    else:
        project.name = name.strip() or project.name
        project.subtitle = subtitle.strip()
        project.link_url = link_url.strip()
    db.commit()
    return _back(f"#project-{project_id}")


@router.post("/bullets/{bullet_id}")
def bullet_update(bullet_id: int, action: str = Form(...), text: str = Form(""), db: Session = Depends(get_db)):
    bullet = db.get(Bullet, bullet_id)
    if bullet is None:
        raise NotFoundError(f"Bullet {bullet_id} not found")
    project_id = bullet.project_id
    if action == "save":
        edit_bullet(db, bullet, text)
    elif action == "approve":
        edit_bullet(db, bullet, text)
        # always re-check (older bullets were stored before the ATS rules)
        bullet.flags = check_bullet(bullet.text, bullet.project.profile_item if bullet.project else None)
        if can_approve(bullet):  # ATS rules and grounding must pass
            bullet.status = "approved"
    elif action == "unapprove":
        bullet.status = "draft"
    elif action == "accept_pending" and bullet.pending_text:
        text = bullet.pending_text
        bullet.pending_text = ""
        edit_bullet(db, bullet, text)
    elif action == "dismiss_pending":
        bullet.pending_text = ""
    elif action == "delete":
        db.delete(bullet)
    db.commit()
    return _back(f"#project-{project_id}")


@router.post("/projects/{project_id}/metric")
def add_metric(project_id: int, answer: str = Form(...), db: Session = Depends(get_db)):
    """The app asked for a metric: the answer becomes an owner fact on the
    project's record, so the next bullet regeneration (and the grounding
    checks) can use it."""
    project = db.get(CVProject, project_id)
    if project is not None and project.profile_item is not None and answer.strip():
        item = project.profile_item
        item.owner_facts = [*(item.owner_facts or []), {"question": "Metric for the CV", "answer": answer.strip(), "source": "owner"}]
        db.commit()
    return _back(f"#project-{project_id}")


@router.post("/skills")
def skills_update(
    action: str = Form(...),
    name: str = Form(""),
    group: str = Form(""),
    skill_id: int = Form(0),
    db: Session = Depends(get_db),
):
    if action == "rebuild":
        rebuild_skills(db)
    elif action == "add" and name.strip():
        add_manual(db, name, group)
    elif action == "import_cv":
        cv = base_cv(db)
        if cv is not None:
            import_cv_skills(db, db.query(ProfileItem).filter(ProfileItem.parent_ref == cv.file_path).all())
            rebuild_skills(db)
    elif skill_id:
        skill = db.get(TechSkill, skill_id)
        if skill is not None:
            if action == "toggle":
                skill.hidden = not skill.hidden
            elif action == "tier":
                skill.tier = "specific" if skill.tier == "core" else "core"
                skill.tier_set_by_owner = True
            elif action == "group" and group in GROUPS:
                skill.group = group
            elif action == "delete":
                db.delete(skill)
            db.commit()
    return _back("#skills")


@router.post("/base-cv")
def set_base_cv(cv_id: int = Form(...), db: Session = Depends(get_db)):
    settings = _settings(db)
    settings.base_cv_id = cv_id
    settings.cv_template_approved = False  # a different base means a different look
    db.commit()
    return _back("#template")


@router.get("/base-pdf")
def base_pdf(db: Session = Depends(get_db)):
    cv = base_cv(db)
    if cv is None or not cv.file_path:
        raise NotFoundError("No base CV")
    return FileResponse(cv.file_path, media_type="application/pdf")


@router.post("/template/sample")
def template_sample(db: Session = Depends(get_db)):
    """A sample CV from the whole bank, to compare with the base CV once."""
    generated = GeneratedCV(selection=assemble(db), language="en")
    db.add(generated)
    db.commit()
    try:
        render_generated(db, generated)
    except RenderUnavailable as exc:
        return RedirectResponse(url=f"/cv-builder?error={exc}", status_code=303)
    return RedirectResponse(url=f"/cv-builder/cvs/{generated.id}?sample=1", status_code=303)


@router.post("/template/approve")
def template_approve(db: Session = Depends(get_db)):
    settings = _settings(db)
    settings.cv_template_approved = True
    db.commit()
    return _back("#template")


@router.post("/for-application/{application_id}")
def tailor_for_application(application_id: int, db: Session = Depends(get_db)):
    application = db.get(Application, application_id)
    if application is None:
        raise NotFoundError(f"Application {application_id} not found")
    generated = create_for_application(db, application)
    return RedirectResponse(url=f"/cv-builder/cvs/{generated.id}", status_code=303)


def _generated_or_404(db: Session, generated_id: int) -> GeneratedCV:
    generated = db.get(GeneratedCV, generated_id)
    if generated is None:
        raise NotFoundError(f"Generated CV {generated_id} not found")
    return generated


@router.get("/cvs/{generated_id}")
def generated_detail(request: Request, generated_id: int, sample: int = 0, error: str = "", db: Session = Depends(get_db)):
    generated = _generated_or_404(db, generated_id)
    selection = generated.selection or {}
    projects = db.query(CVProject).filter(CVProject.selected.is_(True)).order_by(CVProject.sort_order).all()
    skills = db.query(TechSkill).filter(TechSkill.hidden.is_(False)).order_by(TechSkill.sort_order, TechSkill.id).all()
    return render(
        templates,
        request,
        "cv_generated.html",
        {
            "generated": generated,
            "selection": selection,
            "projects": projects,
            "skills_by_group": {g: [s.name for s in skills if s.group == g] for g in GROUPS},
            "group_titles": GROUP_TITLES["en"],
            "groups": GROUPS,
            "locked": locked_sections(db),
            "sample": bool(sample),
            "template_approved": _settings(db).cv_template_approved,
            "error": error,
        },
    )


@router.post("/cvs/{generated_id}")
async def generated_update(request: Request, generated_id: int, db: Session = Depends(get_db)):
    generated = _generated_or_404(db, generated_id)
    form = await request.form()
    project_ids = [v for v in form.getlist("project_ids")]
    bullet_ids: dict[str, list[str]] = {}
    for value in form.getlist("bullet_ids"):
        project_id, _, bullet_id = str(value).partition(":")
        if project_id in project_ids:
            bullet_ids.setdefault(project_id, []).append(bullet_id)
    skills = {g: [str(v) for v in form.getlist(f"skills_{g}")] for g in GROUPS}
    update_selection(
        db,
        generated,
        {
            "project_ids": project_ids,
            "bullet_ids": bullet_ids,
            "skills": skills,
            "certificates": form.get("certificates"),
            "language": form.get("language"),
            "bullet_text": {k.removeprefix("text_"): str(v) for k, v in form.items() if k.startswith("text_")},
            "save_to_bank": {str(v) for v in form.getlist("save_to_bank")},
        },
    )
    if form.get("action") == "render":
        try:
            render_generated(db, generated)
        except RenderUnavailable as exc:
            return RedirectResponse(url=f"/cv-builder/cvs/{generated_id}?error={exc}", status_code=303)
    return RedirectResponse(url=f"/cv-builder/cvs/{generated_id}#pdf", status_code=303)


@router.post("/cvs/{generated_id}/approve")
def generated_approve(generated_id: int, db: Session = Depends(get_db)):
    generated = _generated_or_404(db, generated_id)
    if not _settings(db).cv_template_approved:
        return RedirectResponse(url=f"/cv-builder/cvs/{generated_id}?error=template", status_code=303)
    try:
        approve(db, generated)
    except ValueError:
        return RedirectResponse(url=f"/cv-builder/cvs/{generated_id}?error=ats", status_code=303)
    target = f"/applications/{generated.application_id}" if generated.application_id else f"/cv-builder/cvs/{generated_id}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/cvs/{generated_id}/pdf")
def generated_pdf(generated_id: int, db: Session = Depends(get_db)):
    generated = _generated_or_404(db, generated_id)
    if not generated.pdf_path:
        raise NotFoundError("Not rendered yet")
    return FileResponse(generated.pdf_path, media_type="application/pdf")
