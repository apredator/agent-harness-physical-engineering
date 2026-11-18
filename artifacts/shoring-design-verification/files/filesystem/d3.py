import sys, pypdf
for f in sys.argv[1:]:
    print('='*20, f, '='*20)
    r = pypdf.PdfReader(f)
    for i,p in enumerate(r.pages):
        print('--- page', i+1, '---')
        print(p.extract_text())
