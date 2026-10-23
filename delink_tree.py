#!/usr/bin/env python3
"""把目录树内所有符号链接实体化（fnOS 安装器解 app.tgz 遇链接条目会失败）。
链接目标必须在树内；断链直接报错退出。目录链接展开为真实目录复制。"""
import os, sys, shutil

def main(root):
    root = os.path.abspath(root)
    # 先记录所有链接（含目录链接），自底向上处理避免遍历失真
    links = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                links.append(p)
    n = 0
    for p in sorted(links, key=len, reverse=True):
        tgt = os.path.realpath(p)
        if not os.path.exists(tgt):
            print(f'DANGLING {p} -> {os.readlink(p)}', file=sys.stderr)
            sys.exit(1)
        if not tgt.startswith(root + os.sep):
            print(f'OUTSIDE {p} -> {tgt}', file=sys.stderr)
            sys.exit(1)
        mode = os.lstat(p).st_mode
        os.unlink(p)
        if os.path.isdir(tgt):
            shutil.copytree(tgt, p, symlinks=False, dirs_exist_ok=False)
        else:
            shutil.copy2(tgt, p)
        os.chmod(p, mode & 0o7777)
        n += 1
    # 校验
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            if os.path.islink(os.path.join(dirpath, name)):
                print('REMAINS', os.path.join(dirpath, name), file=sys.stderr)
                sys.exit(1)
    print(f'delinked {n} entries under {root}')

if __name__ == '__main__':
    main(sys.argv[1])
