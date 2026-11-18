from docx import Document
d = Document('/filesystem/Thelbridge_Materials_Acceptance_Record_TEMPLATE.docx')
print('=== PARAGRAPHS ===')
for p in d.paragraphs:
    if p.text.strip():
        print('P|', p.style.name if p.style else '?', '|', p.text)
print('=== TABLES ===')
for ti, t in enumerate(d.tables):
    print('--- table', ti)
    for r in t.rows:
        print(' || '.join(c.text for c in r.cells))
