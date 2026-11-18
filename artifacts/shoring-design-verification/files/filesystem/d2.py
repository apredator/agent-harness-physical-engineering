import openpyxl
wb=openpyxl.load_workbook('Birchwood/PackageA/00_Program/Thelbridge_Verification_Register_PkgA.xlsx',data_only=True)
ws=wb['REGISTER']
rows=list(ws.iter_rows(values_only=True))
for i,r in enumerate(rows[:2]):
    print('HDR',i,r)
for r in rows:
    if r and str(r[0]) in ('048','049'):
        print('====',r[0])
        for j,c in enumerate(r):
            print(' [%d]'%j, repr(c))
