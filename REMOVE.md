# Remove /add-nctop

Delete the tool and its tests:

```bash
rm -f tools/nctop/nctop tools/nctop/test_nctop.py src/nctop.test.ts
rm -rf tools/nctop/__pycache__
rmdir tools/nctop
rmdir tools 2>/dev/null || true
```

If you linked it onto `PATH`, remove the link:

```bash
[ -L ~/.local/bin/nctop ] && rm ~/.local/bin/nctop
```
