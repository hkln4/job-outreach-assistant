// One-page CV in the owner's FlowCV layout (roadmap phase 5). All text comes
// from `sys.inputs.data` as strings, so nothing in it is read as markup.
// Kerning off: with it on, PDF text extraction splits words ("H asan").
// Hyphenation off, and words with a hyphen are boxed so "mid-LTV" never
// breaks across lines (FlowCV's text layer turned it into "midLTV").
#let data = json(bytes(sys.inputs.data))
#set page(paper: "a4", margin: (x: 1.45cm, top: 1.0cm, bottom: 0.9cm))
#set text(font: "Lato", size: data.font_size * 1pt, kerning: false, hyphenate: false, lang: data.lang)
#set par(justify: false, leading: 0.44em, spacing: 0.44em)
#set block(spacing: 0.44em)

#let keep(s) = s.split(" ").map(w => if w.contains("-") { box(w) } else { w }).join(" ")
#let icon(svg) = box(baseline: 12%, image(bytes(svg), format: "svg", height: 0.82em))
#let section(title) = block(above: 0.95em, below: 0.5em, {
  block(below: 0.22em, text(size: 1.27em, weight: "bold", title))
  line(length: 100%, stroke: 0.8pt)
})
#let bullets(items) = for b in items {
  block(above: 0.42em, below: 0.42em, grid(columns: (0.85em, 1fr), [•], keep(b)))
}
#let entry(e, plain) = block(above: 0.7em, below: 0.35em, {
  let second = plain and e.bullets.len() > 0
  let cells = (
    [#text(weight: "bold", e.title)#if e.organization != "" { [, ] + emph(e.organization) }],
    align(right, e.dates),
  )
  if second or e.location != "" {
    cells += (if second { e.bullets.join("\n") } else { [] }, align(right, e.location))
  }
  grid(columns: (1fr, auto), row-gutter: 0.35em, ..cells)
  if not plain and e.bullets.len() > 0 { bullets(e.bullets) }
})

#align(center, block(below: 0.6em, {
  block(below: 0.35em, text(size: 2.2em, weight: "bold", data.name))
  if data.title != "" { block(below: 0.55em, text(size: 1.37em, style: "italic", data.title)) }
  // each item stays whole; the row may wrap between items, never inside one
  text(size: 0.95em, data.contacts.map(c => box[#icon(c.icon)#h(0.3em)#c.text]).join(h(1.1em)))
}))

#for s in data.sections {
  section(s.title)
  if s.kind == "education" { for e in s.entries { entry(e, true) } }
  if s.kind == "experience" { for e in s.entries { entry(e, false) } }
  if s.kind == "skills" {
    for g in s.groups {
      block(above: 0.5em, below: 0.25em, text(weight: "bold", g.title))
      block(above: 0.2em, below: 0.4em, keep(g.items.join(", ")))
    }
  }
  if s.kind == "projects" {
    for p in s.projects {
      block(above: 0.55em, below: 0.25em, {
        text(weight: "bold", p.title)
        if p.link != "" { h(0.35em) + link(p.link, icon(data.link_icon)) }
      })
      bullets(p.bullets)
    }
  }
  if s.kind == "certificates" {
    for c in s.items { block(above: 0.45em, below: 0.3em, text(weight: "bold", c)) }
  }
}
