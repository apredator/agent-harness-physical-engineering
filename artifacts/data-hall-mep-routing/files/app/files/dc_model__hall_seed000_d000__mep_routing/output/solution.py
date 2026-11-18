#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import ifcopenshell
import ifcopenshell.guid as g

OUT = sys.argv[1] if len(sys.argv) > 1 else "output/model.ifc"
f = ifcopenshell.file(schema="IFC4")
def gu(): return g.create()

len_u = f.createIfcSIUnit(None, "LENGTHUNIT", "MILLI", "METRE")
area_u = f.createIfcSIUnit(None, "AREAUNIT", None, "SQUARE_METRE")
vol_u = f.createIfcSIUnit(None, "VOLUMEUNIT", None, "CUBIC_METRE")
units = f.createIfcUnitAssignment([len_u, area_u, vol_u])
ctx = f.createIfcGeometricRepresentationContext(None, "Model", 3, 1.0e-5, f.createIfcAxis2Placement3D(f.createIfcCartesianPoint([0.0,0.0,0.0]), None, None), None)
body = f.createIfcGeometricRepresentationSubContext("Body","Model",None,None,None,None,ctx,None,"MODEL_VIEW",None)

proj = f.createIfcProject(gu(), None, "DataHall", None, None, None, None, [ctx], units)
site = f.createIfcSite(gu(), None, "Site", None, None, None, None, None, None, None, None, None)
bldg = f.createIfcBuilding(gu(), None, "Building", None, None, None, None, None, None, None, None, None)
storey = f.createIfcBuildingStorey(gu(), None, "Storey", None, None, None, None, None, None, 0.0)
f.createIfcRelAggregates(gu(), None, None, None, proj, [site])
f.createIfcRelAggregates(gu(), None, None, None, site, [bldg])
f.createIfcRelAggregates(gu(), None, None, None, bldg, [storey])

contained = []
def place(x, y, z_m):
    return f.createIfcAxis2Placement3D(f.createIfcCartesianPoint([float(x)/1000.0, float(y)/1000.0, float(z_m)]), None, None)

def box(name, x, y, z, L, W, H, cls="IfcBuildingElementProxy", ptype=None, ot=None):
    prof = f.createIfcRectangleProfileDef("AREA", None, f.createIfcAxis2Placement2D(f.createIfcCartesianPoint([0.0,0.0]), None), float(L), float(W))
    solid = f.createIfcExtrudedAreaSolid(prof, place(0,0,0), f.createIfcDirection([0.0,0.0,1.0]), float(H))
    shp = f.createIfcShapeRepresentation(body, "Body", "SweptSolid", [solid])
    prod = f.createIfcProductDefinitionShape(None, None, [shp])
    e = f.create_entity(cls, gu(), None, name, None, None, place(x,y,z), prod, None, ptype) if cls != "IfcFurniture" else f.createIfcFurniture(gu(), None, name, None, None, place(x,y,z), prod, None, ptype)
    if ot:
        e.ObjectType = ot
    contained.append(e)
    return e

HX, HY = 27000.0, 21000.0
# floor
box("AccessFloor", HX/2, HY/2, -0.05, HX, HY, 0.05)
# structural columns grid
for cx in (0.0, HX):
    for cy in (0.0, HY):
        box("Column", cx, cy, 0.0, 400, 400, 3400, cls="IfcColumn")
# rows of racks
row_y = [3000.0, 6000.0, 9000.0, 12000.0, 15000.0, 18000.0]
for ry in row_y:
    x = 4000.0
    while x < HX - 3000.0:
        box("Rack", x, ry, 0.0, 600, 1000, 2000, cls="IfcFurniture", ptype="USERDEFINED", ot="Rack")
        x += 700.0
# CRAHs (perimeter)
for cx, cy in [(1000.0, HY-800.0), (HX/2, HY-800.0), (HX-1000.0, HY-800.0), (1000.0, 800.0)]:
    box("CRAH", cx, cy, 0.0, 1800, 800, 2000, cls="IfcBuildingElementProxy", ot="CRAH")
# PDUs
for cx, cy in [(2000.0, 10500.0), (HX-2000.0, 10500.0)]:
    box("PDU", cx, cy, 0.0, 800, 1000, 2000, cls="IfcBuildingElementProxy", ot="PDU")
# sprinkler heads
for x in range(2000, int(HX), 4000):
    for y in range(2000, int(HY), 4000):
        box("Sprinkler", x, y, 3250.0, 100, 100, 100, cls="IfcBuildingElementProxy", ot="Sprinkler")

# ---- Routing ----
TRAY_Z, BUS_Z, CHW_Z = 2600.0, 2900.0, 2400.0
def seg(ot, name, x0, y0, z0, x1, y1, z1, w, h):
    if abs(y1-y0) < 1e-6:
        L, W = abs(x1-x0), w
    else:
        L, W = w, abs(y1-y0)
    cx, cy = (x0+x1)/2.0, (y0+y1)/2.0
    return box(name, cx, cy, min(z0,z1), L, W, h, cls="IfcBuildingElementProxy", ot=ot)

# Tray backbone + row branches
seg("CableTray","Tray_Main", 500, 1000, TRAY_Z, HX-1500, 1000, TRAY_Z, 600, 100)
for ri, ry in enumerate(row_y):
    seg("CableTray","Tray_Conn_%d"%ri, 2500, 1000, TRAY_Z, 2500, ry, TRAY_Z, 400, 100)
    seg("CableTray","Tray_Branch_%d"%ri, 2500, ry, TRAY_Z, HX-2500, ry, TRAY_Z, 600, 100)

# Busway (300 mm separation from tray on y)
seg("Busway","Bus_Main", 500, 2000, BUS_Z, HX-1500, 2000, BUS_Z, 500, 150)
for ri, ry in enumerate(row_y):
    seg("Busway","Bus_Branch_%d"%ri, 2000, ry, BUS_Z, HX-2000, ry, BUS_Z, 500, 150)
    seg("Busway","Bus_Conn_%d"%ri, 2000, 2000, BUS_Z, 2000, ry, BUS_Z, 500, 150)

# CHW main + branches to rows and CRAHs
seg("CHW","CHW_Main", 500, 1500, CHW_Z, HX-1500, 1500, CHW_Z, 150, 150)
for ri, ry in enumerate(row_y):
    seg("CHW","CHW_Branch_%d"%ri, 3000, 1500, CHW_Z, 3000, ry, CHW_Z, 150, 150)
    seg("CHW","CHW_Row_%d"%ri, 3000, ry, CHW_Z, HX-3000, ry, CHW_Z, 150, 150)
for ci,(cx,cy) in enumerate([(1000.0, HY-800.0),(HX/2, HY-800.0),(HX-1000.0, HY-800.0),(1000.0, 800.0)]):
    seg("CHW","CHW_CRAH_%d"%ci, cx, 1500, CHW_Z, cx, cy, CHW_Z, 150, 150)

f.createIfcRelContainedInSpatialStructure(gu(), None, None, None, [c for c in contained if c.is_a("IfcProduct")], storey)
f.write(OUT)
print("wrote", OUT, "elements", len(contained))
