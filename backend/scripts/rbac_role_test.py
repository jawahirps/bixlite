"""RBAC check: temporary users per role hit key endpoints; prints PASS/FAIL. Run from backend/: PYTHONPATH=. python scripts/rbac_role_test.py (backend must be running)."""
from app.database import SessionLocal
from app.models import User, UserFacilityAccess
from app.security import create_access_token, hash_password
import requests
from sqlalchemy import text
db=SessionLocal(); B="http://localhost:8000/api"
def mk(email, role, fac=(), grp=()):
    u=User(email=email, full_name=email, hashed_password=hash_password("x"*12), role=role, is_active=True)
    db.add(u); db.flush()
    for f in fac: db.add(UserFacilityAccess(user_id=u.id, facility_id=f))
    for g in grp: db.add(UserFacilityAccess(user_id=u.id, group_id=g))
    db.commit(); return u, {"Authorization":"Bearer "+create_access_token({"sub":str(u.id)})}
ids=[]
try:
    fa,FA=mk("t.fa@x.test","facility_admin",fac=[13]); ids.append(fa.id)
    ga,GA=mk("t.ga@x.test","group_admin",grp=[1]); ids.append(ga.id)
    vw,VW=mk("t.vw@x.test","viewer",fac=[24]); ids.append(vw.id)
    st,ST=mk("t.st@x.test","staff",fac=[13,14]); ids.append(st.id)
    sid_other=db.execute(text("select id from scrub_sessions where facility_id<>13 limit 1")).scalar()
    rx_other=db.execute(text("select id from raw_xml_store where facility_id<>13 limit 1")).scalar()
    rx_own=db.execute(text("select id from raw_xml_store where facility_id=13 limit 1")).scalar()
    T=[
     ("FA own facility worklist",FA,"get","/resub/worklist?facility_id=13",200),
     ("FA other facility worklist",FA,"get","/resub/worklist?facility_id=14",403),
     ("FA ra21 other",FA,"get","/resub-prepare/ra21?facility_id=5",403),
     ("FA cycle no facility -> own",FA,"get","/dashboard/cycle",200),
     ("FA raw xml other record",FA,"get",f"/dha-sync/raw-xml/{rx_other}/peek",403),
     ("FA raw xml own record",FA,"get",f"/dha-sync/raw-xml/{rx_own}/peek",200),
     ("FA scrub session other",FA,"get",f"/scrub/sessions/{sid_other}/results",403),
     ("FA alerts",FA,"get","/alerts",200),
     ("FA facilities admin page",FA,"get","/facilities",403),
     ("FA json body other",FA,"post","/resub/tracking",403,{"facility_id":14,"updates":[]}),
     ("GA Alnoor Qusais",GA,"get","/calibration/file-denial-summary?facility_id=7",200),
     ("GA Apollo",GA,"get","/calibration/file-denial-summary?facility_id=13",403),
     ("GA turnaround no facility (5 facs)",GA,"get","/turnaround/summary",400),
     ("ST multi, no facility",ST,"get","/denial-trends/monthly",400),
     ("ST We Care",ST,"get","/denial-trends/monthly?facility_id=14",200),
     ("VW analytics summary",VW,"get","/analytics/submissions",200),
     ("VW post blocked",VW,"post","/resub/tracking",403,{"facility_id":24,"updates":[]}),
     ("VW other facility",VW,"get","/dashboard/cycle?facility_id=13",403),
    ]
    bad=0
    for name,H,m,url,exp,*body in T:
        r=getattr(requests,m)(B+url,headers=H,json=body[0] if body else None,timeout=300)
        ok = r.status_code==exp
        bad+= not ok
        print(("PASS" if ok else "FAIL"), r.status_code, "exp", exp, name, "" if ok else r.text[:150])
    a=requests.get(B+"/alerts",headers=FA,timeout=300).json()
    print("FA alerts facilities:", sorted({x["facility_id"] for x in a["items"] if x["facility_id"]}))
    f=requests.get(B+"/analytics/filters",headers=GA,timeout=300).json()["facilities"]
    print("GA filter facilities:", [x["name"] for x in f])
    s=requests.get(B+"/analytics/submissions",headers=VW,timeout=300).json()
    print("VW submissions keys:", list(s)[:5])
    print("FAILED:",bad)
finally:
    db.rollback()
    db.execute(text("delete from login_logs where user_id = any(:i)"),{"i":ids})
    db.execute(text("delete from report_jobs where user_id = any(:i)"),{"i":ids})
    db.execute(text("delete from users where id = any(:i)"),{"i":ids}); db.commit(); print("cleaned", ids)
