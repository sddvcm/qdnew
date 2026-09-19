import re

frag = '验证码错误，请重新输入</div>\n<a href="/registe'
f = re.sub(r'<[^>]+>', '', frag)
print('after strip tags  :', repr(f))

variants = {
    r'<[^>\s]*$  (M)': (r'<[^>\s]*$', re.M),
    r'<[^>\s]*\Z    ': (r'<[^>\s]*\Z', 0),
    r'<[^<>]*$   (M)': (r'<[^<>]*$', re.M),
    r'<.*$      (M)': (r'<.*$', re.M),
}
for name, (pat, fl) in variants.items():
    out = re.sub(pat, '', f, flags=fl)
    print('%-20s -> %r' % (name, out))

# 逐行处理版本
lines = []
for ln in f.split('\n'):
    ln = re.sub(r'<[^>]*>', '', ln)
    ln = re.sub(r'^\S{0,20}>', '', ln)
    ln = ln.split('<')[0] if '<' in ln else ln
    lines.append(ln)
print('linewise            ->', repr(' '.join(lines)))
