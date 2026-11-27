#!/usr/bin/env python3
"""Remove user-facing profile controls from integrated offline preview."""
import argparse, pathlib, re
p=argparse.ArgumentParser()
p.add_argument('input',type=pathlib.Path);p.add_argument('output',type=pathlib.Path)
p.add_argument('--fixture',type=pathlib.Path,help='Optional standalone entitlement test page')
a=p.parse_args();s=a.input.read_text()
if a.fixture:
    a.fixture.parent.mkdir(parents=True,exist_ok=True)
    a.fixture.write_text(s.replace('<title>', '<title>ADMIN TEST FIXTURE · ',1))
def replace(old,new):
    global s
    if s.count(old)!=1: raise ValueError(f'Expected exactly one occurrence of {old[:80]!r}; got {s.count(old)}')
    s=s.replace(old,new,1)
def pattern(rx,new):
    global s
    s,n=re.subn(rx,new,s,flags=re.S)
    if n!=1: raise ValueError(f'Expected one regex match of {rx[:80]!r}; got {n}')
pattern(r'<details class="ent-fold">.*?</section></details>','')
pattern(r'<span class="viewlabel">Workspace lens</span><div class="role-tabs".*?</div><span class="view-hint" id="roleHint"></span>','')
replace('/* Synthetic presentation experiment. The selected lens is not a security role. */','''/* Preview profile is assigned by session, not user-selectable.
 Production must substitute server-validated session identity and permissions. */
const assignedProfile=Object.freeze({tenant:'north',persona:'owner',layout:'executive'});''')
replace("let lens='executive',presetsByLens={},editingLayout=false,evidencePath=[],evidencePage=0;","const lens=assignedProfile.layout;let presetsByLens={},editingLayout=false,evidencePath=[],evidencePage=0;")
replace("const STORE='bix-lite-glass-workspace-v1';","const STORE='bix-lite-glass-workspace-v2:'+assignedProfile.tenant+':'+assignedProfile.persona;")
replace("if(layoutDefaults[saved.lens])lens=saved.lens;","")
replace("JSON.stringify({lens,layouts:presetsByLens})","JSON.stringify({layouts:presetsByLens})")
replace("""document.querySelectorAll('[data-role]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.role===lens)));$('roleHint').textContent=hints[lens];""",'')
pattern(r"document\.querySelectorAll\('\[data-role\]'\)\.forEach\(b=>b\.onclick=.*?\);\nconst drillStages=", "const drillStages=")
replace("let tenant='north',person='owner',current='dashboard',epoch=0;","const tenant=assignedProfile.tenant,person=assignedProfile.persona;let current='dashboard',epoch=0;")
replace("No real authentication or subscription is connected. Switch the synthetic demo persona to explore another entitlement.","This feature is not available to your assigned profile.")
replace("Included in this synthetic persona","Included in your assigned profile")
replace("['entAudit','dashboard.audit'],","")
pattern(r" \$\('entScope'\)\.textContent=.*?\n \$\('entSummary'\)\.textContent=.*?;\n","")
pattern(r"\$\('entTenant'\)\.onchange=.*?\nresetScope\(\);", "resetScope();")
replace("const assignedProfile=Object.freeze({tenant:'north',persona:'owner',layout:'executive'});","""const assignedProfile=Object.freeze({tenant:'north',persona:'owner'});
const assignedLayouts=Object.freeze({owner:'executive',analyst:'analyst',specialist:'sme',viewer:'executive'});""")
replace("const lens=assignedProfile.layout;","const lens=assignedLayouts[assignedProfile.persona]||'executive';")
a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(s)
