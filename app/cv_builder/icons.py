"""Small monochrome SVG icons for the CV contact row (drawn as images, so they
add nothing to the PDF's text layer)."""

def _svg(body: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#222" '
        'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' + body + "</svg>"
    )


ICONS = {
    "email": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#222" d="M2 5h20v14H2z"/><path d="M2.5 5.5 12 13l9.5-7.5" fill="none" stroke="#fff" stroke-width="2"/></svg>',
    "linkedin": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="1" y="1" width="22" height="22" rx="3" fill="#222"/><path fill="#fff" d="M5 9.5h3V19H5zM6.5 5a1.7 1.7 0 1 1 0 3.4 1.7 1.7 0 0 1 0-3.4zM10 9.5h2.9v1.3c.5-.9 1.6-1.6 3.1-1.6 3 0 3.5 2 3.5 4.5V19h-3v-4.6c0-1.1 0-2.5-1.6-2.5s-1.9 1.2-1.9 2.4V19h-3z"/></svg>',
    "github": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#222" d="M12 1a11 11 0 0 0-3.5 21.4c.6.1.8-.2.8-.6v-2c-3.1.7-3.7-1.5-3.7-1.5-.5-1.3-1.2-1.6-1.2-1.6-1-.7.1-.7.1-.7 1.1.1 1.7 1.1 1.7 1.1 1 1.7 2.6 1.2 3.2.9.1-.7.4-1.2.7-1.5-2.4-.3-5-1.2-5-5.4 0-1.2.4-2.2 1.1-3-.1-.3-.5-1.4.1-2.9 0 0 .9-.3 3 1.1a10.4 10.4 0 0 1 5.5 0c2.1-1.4 3-1.1 3-1.1.6 1.5.2 2.6.1 2.9.7.8 1.1 1.8 1.1 3 0 4.2-2.6 5.1-5 5.4.4.3.7 1 .7 2v3c0 .4.2.7.8.6A11 11 0 0 0 12 1z"/></svg>',
    "phone": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#222" d="M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1z"/></svg>',
    "city": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#222" d="M12 1a8 8 0 0 0-8 8c0 6 8 14 8 14s8-8 8-14a8 8 0 0 0-8-8zm0 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6z"/></svg>',
    "portfolio": _svg('<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20"/>'),
    "link": _svg('<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>'),
}
