# Contributing to Side

The whole product is one file: [`app/index.html`](app/index.html). Open it, edit it, refresh your browser. That's the entire dev loop — no install, no build, no toolchain.

## The three rules

They're what keep Side a single file anyone can hack on:

1. **Zero dependencies.** No frameworks, no CDN scripts, no imports. Everything lives in the one file. Images are inlined data URIs.
2. **ES5 only.** `var` and `function` — no arrow functions, no template literals, no `const`/`let`, no optional chaining. The file must parse everywhere.
3. **ASCII-only source.** Use `…`-style escapes in JS strings and HTML entities (`&mdash;`, `&rsquo;`) in markup.

## Checks before you open a PR

```sh
# every <script> block must parse
python3 - app/index.html <<'PY'
import re,sys,subprocess,tempfile,os
h=open(sys.argv[1],encoding='utf-8').read()
for i,s in enumerate(re.findall(r'<script[^>]*>(.*?)</script>',h,re.S)):
    f=tempfile.NamedTemporaryFile('w',suffix='.js',delete=False,encoding='utf-8');f.write(s);f.close()
    r=subprocess.run(['node','--check',f.name],capture_output=True,text=True)
    if r.returncode: print('FAIL block',i,r.stderr.splitlines()[0])
    os.unlink(f.name)
print('non-ascii chars:',sum(1 for c in h if ord(c)>127))
PY
```

Then open the app and click through what you touched — the console must stay clean.

## Good first contributions

- New templates for the template library
- New widgets for the fleet board
- Polish on any drawer or flow
- The big one: the real execution engine (see the roadmap in the README)

## Filing issues

Use the issue templates — **Bug** for something broken or off, **Idea** for something Side should do.
