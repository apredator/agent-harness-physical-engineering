import openpyxl
wb=openpyxl.load_workbook('Birchwood/PackageA/00_Program/Thelbridge_Verification_Register_PkgA.xlsx',data_only=True)
for sn in ['REFERENCE VALUES','REGISTER']:
    ws=wb[sn]; print('=====',sn)
    for r in ws.iter_rows(values_only=True):
        if any(c is not None for c in r): print(r)
