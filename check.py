"""Run before every upload: python check.py  (checks Python files and the website's JavaScript)"""
import ast, re, shutil, subprocess, sys, tempfile
ok = True
for f in ("server.py", "engine.py", "strategy.py", "brokers.py", "replay.py"):
    try: ast.parse(open(f, encoding="utf-8").read()); print("OK  ", f)
    except SyntaxError as e: ok = False; print("FAIL", f, e)
h = open("static/index.html", encoding="utf-8").read(); js = h[h.index("<script>") + 8:h.rindex("</script>")]
if shutil.which("node"):
    t = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8"); t.write(js); t.close()
    r = subprocess.run(["node", "--check", t.name], capture_output=True, text=True)
    print("OK   static/index.html (JavaScript)" if r.returncode == 0 else "FAIL static/index.html\n" + r.stderr[:800]); ok &= r.returncode == 0
else: print("SKIP JavaScript check (Node.js not installed)")
sys.exit(0 if ok else 1)
