#!/usr/bin/env python3
import sys
import ifcopenshell
import ifcopenshell.guid as guid

HX, HY = 27000.0, 24000.0
COL, CH, FF, MINHR = 400.0, 6000.0, 0.0, 2600.0
SPRZ = 4200.0
TILE = 600.0
row_ys = [1200.0,3600.0,6000.0,9000.0,11400.0,13800.0,16200.0,18600.0]
counts = [12,12,12,11,12,12,12,11]
rz = 1200.0
TRAY_W = 600.0; BUS_W = 200.0; PIPE_D = 150.0
TRAY_Z, BUS_Z, PIPE_Z = 3000.0, 3400.0, 2800.0


def box(f, x0,y0,z0,x1,y1,z1):
    pts=[f.createIfcCartesianPoint((float(a),float(b),float(c))) for a,b,c in
         [(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),(x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1)]]
    faces=[]
    for i,j,k in [(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),(1,5,6),(1,6,2),(2,6,7),(2,7,3),(3,7,4),(3,4,0)]:
        loop=f.createIfcPolyLoop([pts[i],pts[j],pts[k]])
        bound=f.createIfcFaceOuterBound(loop,True)
        faces.append(f.createIfcFace([bound]))
    return f.createIfcFacetedBrep(f.createIfcClosedShell(faces))


def main():
    out = sys.argv[1] if len(sys.argv)>1 else 'output/model.ifc'
    f = ifcopenshell.file(schema='IFC4')
    origin=f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((0.,0.,0.)))
    ctx=f.createIfcGeometricRepresentationContext(None,'Model',3,1e-5,origin,None)

    units=f.createIfcUnitAssignment([
        f.createIfcSIUnit(None,'LENGTHUNIT','MILLI','METRE'),
    ])
    proj=f.createIfcProject(guid.new(),None,'h',None,None,None,None,[ctx],units)
    site=f.createIfcSite(guid.new(),None,'Site',None,None,
        f.createIfcLocalPlacement(None,origin),None,None,'ELEMENT',None,None,None,None,None,None,None)
    bld=f.createIfcBuilding(guid.new(),None,'Building',None,None,
        f.createIfcLocalPlacement(None,origin),None,None,'ELEMENT',None,None,None,None,None)
    st=f.createIfcBuildingStorey(guid.new(),None,'Storey',None,None,
        f.createIfcLocalPlacement(None,origin),None,None,'ELEMENT',None,FF)

    f.createIfcRelAggregates(guid.new(),None,None,None,proj,[site])
    f.createIfcRelAggregates(guid.new(),None,None,None,site,[bld])
    f.createIfcRelAggregates(guid.new(),None,None,None,bld,[st])

    prods=[]
    def mk(cls, name, x0,y0,z0,x1,y1,z1, otype=None):
        rep=f.createIfcShapeRepresentation(ctx,'Body','SweptSolid',[box(f,x0,y0,z0,x1,y1,z1)])
        pds=f.createIfcProductDefinitionShape(None,None,[rep])
        pl=f.createIfcLocalPlacement(None,f.createIfcAxis2Placement3D(
            f.createIfcCartesianPoint((x0*0.001,y0*0.001,z0*0.001))))
        p=f.create_entity(cls, GlobalId=guid.new(), OwnerHistory=None, Name=name,
                          ObjectType=otype, ObjectPlacement=pl, Representation=pds)
        prods.append(p)
        return p

    # columns - 7200 grid
    gx=0.0
    while gx<=HX:
        gy=0.0
        while gy<=HY:
            cx=min(max(gx,0.0),HX-COL); cy=min(max(gy,0.0),HY-COL)
            mk('IfcColumn','C',cx,cy,FF,cx+COL,cy+COL,FF+CH)
            gy+=7200.0
        gx+=7200.0

    # racks
    for r,(ry,cnt) in enumerate(zip(row_ys,counts)):
        for i in range(cnt):
            cx=1200.0+i*TILE; cy=ry
            mk('IfcFurniture','Rack',cx-300,cy-500,FF,cx+300,cy+500,FF+rz,'Rack')

    # CRAHs perimeter + PDU
    for i in range(4):
        x=3000.0+i*6000.0
        mk('IfcBuildingElementProxy','CRAH',x-500,HY-1200,FF,x+500,HY-200,FF+2000,'CRAH')
    for i in range(4):
        x=3000.0+i*6000.0
        mk('IfcBuildingElementProxy','PDU',x-400,300,FF,x+400,1100,FF+2000,'PDU')

    # sprinklers
    for i in range(13):
        for j in range(8):
            x=500+i*2000.0; y=1500+j*3000.0
            if x<HX and y<HY:
                mk('IfcBuildingElementProxy','Spr',x-40,y-40,SPRZ-80,x+40,y+40,SPRZ,'Sprinkler')

    # services
    ytray=row_ys[0]+2000.0; ybus=ytray+700.0; ypipe=row_ys[1]+2000.0
    mk('IfcCableCarrierSegment','Tray',0,ytray-TRAY_W/2,TRAY_Z,HX,ytray+TRAY_W/2,TRAY_Z+40,'CableTray')
    mk('IfcCableCarrierSegment','Busway',0,ybus-BUS_W/2,BUS_Z,HX,ybus+BUS_W/2,BUS_Z+60,'Busway')
    mk('IfcPipeSegment','CHW',0,ypipe-PIPE_D/2,PIPE_Z,HX,ypipe+PIPE_D/2,PIPE_Z+PIPE_D,'ChilledWater')

    f.createIfcRelContainedInSpatialStructure(guid.new(),None,None,None,prods,st)
    f.write(out)

if __name__=='__main__':
    main()
