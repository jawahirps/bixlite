"""RBAC sweep: every GET endpoint taking facility_id, called as an Apollo Facility Admin for another facility, must return 403. Run from backend/: PYTHONPATH=. python scripts/rbac_route_sweep.py"""
from app.main import app
from app.database import SessionLocal
from app.models import User, UserFacilityAccess
from app.security import create_access_token, hash_password
from fastapi.routing import APIRoute
from fastapi.dependencies.utils import get_flat_dependant
from sqlalchemy import text
import requests
db=SessionLocal(); B="http://localhost:8000"
u=User(email="t.sweep@x.test", full_name="sweep", hashed_password=hash_password("x"*12), role="facility_admin", is_active=True)
db.add(u); db.flush(); db.add(UserFacilityAccess(user_id=u.id, facility_id=13)); db.commit()
H={"Authorization":"Bearer "+create_access_token({"sub":str(u.id)})}
adm=db.query(User).filter(User.role=="admin").first(); AH={"Authorization":"Bearer "+create_access_token({"sub":str(adm.id)})}
leaks=[]; n=0
try:
    for r in app.routes:
        if not isinstance(r,APIRoute) or r.path.startswith("/api/portal") or r.path.startswith("/api/auth"): continue
        flat=get_flat_dependant(r.dependant)
        names={p.name for p in flat.query_params+flat.path_params}
        if "facility_id" not in names: continue
        if "{" in r.path.replace("{facility_id}",""): continue  # other path ids handled by record test
        url=B+r.path.replace("{facility_id}","14")
        params={"facility_id":14} if "{facility_id}" not in r.path else {}
        for p in flat.query_params:  # fill required params with harmless values
            if p.required and p.name!="facility_id": params[p.name]={"month":"2026-10","code":"X","clinician":"X","kind":"clinician_tests","claim_id":"X","id":"1"}.get(p.name,"1")
        m=list(r.methods)[0].lower()
        if m!="get": continue
        n+=1
        res=requests.get(url,headers=H,params=params,timeout=300)
        if res.status_code not in (403,):
            leaks.append((r.path,res.status_code,res.text[:80]))
    print("GET endpoints with facility_id swept:",n,"| not 403:",len(leaks))
    for l in leaks: print("  ",l)
    for path in ["/api/resub/worklist?facility_id=14","/api/alerts","/api/analytics/submissions","/api/dashboard/cycle","/api/facilities"]:
        print("admin",path,requests.get(B+path,headers=AH,timeout=300).status_code)
finally:
    db.execute(text("delete from users where id=:i"),{"i":u.id}); db.commit()
