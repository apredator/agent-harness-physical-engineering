import openpyxl, sys
for f in sys.argv[1:]:
    print('##### FILE:', f)
    wb=openpyxl.load_workbook(f,data_only=True)
    for ws in wb:
        print('=== SHEET:',ws.title, ws.dimensions)
        for row in ws.iter_rows(values_only=True):
            if any(c not in (None,'') for c in row):
                print(row)
