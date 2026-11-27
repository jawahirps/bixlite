#!/usr/bin/env python3
import pathlib,sys
source,target=map(pathlib.Path,sys.argv[1:3])
root=pathlib.Path(__file__).parent
s=source.read_text()
def rep(a,b):
 global s
 if s.count(a)!=1: raise ValueError(f'missing/ambiguous: {a[:80]}')
 s=s.replace(a,b,1)
rep('>Resize widgets</button>','>Edit layout</button>')
rep("const key=['paid','denied','resubmitted','awaiting'][i];if(!key)return;","const key=node.dataset.gestureWidget||['paid','denied','resubmitted','awaiting'][i];if(!key)return;node.dataset.gestureWidget=key;")
rep("editingLayout?'Done resizing':'Resize widgets'","editingLayout?'Done':'Edit layout'")
rep('</head>','<style id="widget-gesture-css">'+(root/'gestures.css').read_text()+'</style></head>')
rep('</body>','<script id="widget-gesture-js">'+(root/'gestures.js').read_text()+'</script></body>')
target.write_text(s)
