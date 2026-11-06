"""Bilingual term normalization for code-side matching (CLAUDE.md section 8:
deterministic first). Company text is often Turkish and profile records are
English; "bilgisayarlı görü" must meet "computer vision" before any model is
asked. Used by the evidence pre-rank, the role-type guess and (phase 5) the
posting keyword match.
"""
import re
import unicodedata

# canonical concept -> variants (any language). Canonical names are lowercase.
# Ambiguous short forms are left out on purpose: "CV" is a résumé in Turkish
# job text, "vision" is in every mission statement.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "computer vision": ("bilgisayarlı görü", "bilgisayarli goru", "görüntü işleme", "goruntu isleme", "image processing"),
    "machine learning": ("makine öğrenmesi", "makine ogrenmesi", "makine öğrenimi", "ml"),
    "deep learning": ("derin öğrenme", "derin ogrenme", "neural network", "sinir ağı", "yapay sinir ağları"),
    "artificial intelligence": ("yapay zeka", "yapay zekâ", "ai"),
    "natural language processing": ("doğal dil işleme", "dogal dil isleme", "nlp", "metin madenciliği", "text mining"),
    "generative ai": ("üretken yapay zeka", "uretken yapay zeka", "genai", "llm", "large language model", "büyük dil modeli", "buyuk dil modeli", "rag"),
    "object detection": ("nesne tespiti", "nesne algılama", "nesne tanıma", "detection", "yolo"),
    "segmentation": ("segmentasyon", "bölütleme", "boluteleme", "u-net", "unet"),
    "medical imaging": ("tıbbi görüntüleme", "tibbi goruntuleme", "medical image", "radyoloji", "radiology", "dicom"),
    "speech": ("konuşma tanıma", "ses işleme", "speech recognition", "tts", "text-to-speech", "asr", "audio"),
    "time series": ("zaman serisi", "zaman serileri", "forecasting", "tahminleme"),
    "recommendation": ("öneri sistemi", "tavsiye sistemi", "recommender"),
    "data engineering": ("veri mühendisliği", "veri muhendisligi", "etl", "elt", "data pipeline", "veri hattı", "veri boru hattı"),
    "data analysis": ("veri analizi", "veri analitiği", "analytics", "data analytics", "iş zekası", "business intelligence"),
    "big data": ("büyük veri", "buyuk veri", "spark", "hadoop"),
    "backend": ("arka uç", "back-end", "back end", "sunucu tarafı", "server-side", "api", "rest api", "microservice", "mikroservis"),
    "frontend": ("ön yüz", "front-end", "front end", "arayüz", "user interface"),
    "database": ("veritabanı", "veri tabanı", "sql", "postgresql", "mysql", "mongodb"),
    "cloud": ("bulut", "aws", "gcp", "google cloud", "azure"),
    "mlops": ("ml ops", "model deployment", "model dağıtımı", "model serving", "mlflow", "kubeflow"),
    "devops": ("ci/cd", "docker", "kubernetes", "konteyner", "container"),
    "embedded": ("gömülü", "gomulu", "embedded systems", "firmware", "mikrodenetleyici"),
    "mobile": ("mobil", "android", "ios", "flutter", "react native"),
    "web": ("web uygulaması", "web geliştirme", "web development"),
    "robotics": ("robotik", "ros", "otonom", "autonomous"),
    "remote sensing": ("uzaktan algılama", "uydu görüntüsü", "satellite imagery", "gis", "coğrafi bilgi sistemleri"),
    "cybersecurity": ("siber güvenlik", "siber guvenlik", "security", "güvenlik"),
    "python": ("python",),
    "pytorch": ("pytorch", "torch"),
    "tensorflow": ("tensorflow", "keras"),
    "opencv": ("opencv", "cv2"),
}

ROLE_TYPES = ("ml_ai", "computer_vision", "backend_data", "mlops")
ROLE_CONCEPTS = {
    "computer_vision": {"computer vision", "object detection", "segmentation", "medical imaging", "opencv", "remote sensing"},
    "ml_ai": {"machine learning", "deep learning", "artificial intelligence", "natural language processing", "generative ai", "speech", "recommendation", "time series", "pytorch", "tensorflow"},
    "backend_data": {"backend", "database", "data engineering", "big data", "data analysis", "web", "cloud"},
    "mlops": {"mlops", "devops"},
}

_WORD_RE = re.compile(r"[\w+#.\-]+", re.UNICODE)


def fold(text: str) -> str:
    """Casefold and strip Turkish diacritics, so "Görüntü" == "goruntu"."""
    text = (text or "").replace("ı", "i").replace("İ", "i")
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


_FOLDED = {concept: tuple(fold(v) for v in (concept, *variants)) for concept, variants in SYNONYMS.items()}


def concepts_in(text: str) -> set[str]:
    """Canonical concepts mentioned in `text` (whole-word / phrase match)."""
    folded = f" {' '.join(_WORD_RE.findall(fold(text)))} "
    found = set()
    for concept, variants in _FOLDED.items():
        for variant in variants:
            phrase = " ".join(_WORD_RE.findall(variant))
            if phrase and f" {phrase} " in folded:
                found.add(concept)
                break
    return found


def tokens(text: str) -> set[str]:
    """Folded words of 3+ characters, for plain lexical overlap."""
    return {w for w in _WORD_RE.findall(fold(text)) if len(w) >= 3}


def role_type_for(text: str) -> str:
    """The role type a position/company text points at, by concept overlap.
    Defaults to ml_ai (the owner's main target) when nothing matches."""
    concepts = concepts_in(text)
    scores = {role: len(concepts & wanted) for role, wanted in ROLE_CONCEPTS.items()}
    best = max(scores, key=lambda role: (scores[role], role == "ml_ai"))
    return best if scores[best] else "ml_ai"
