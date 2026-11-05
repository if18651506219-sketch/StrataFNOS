#!/usr/bin/env python3
"""把目录树内所有符号链接与硬链接实体化（fnOS 安装器解 app.tgz 遇链接条目会
报"解压app.tgz失败"）。

链接目标必须在树内；断链直接报错退出。目录链接展开为真实目录复制。
0.2.10 修复：旧版只处理软链接。AppImage squashfs 里 `cp -a` 保留下来的
硬链接（同一 inode 出现两次）会被 tar 写成 LNKTYPE 条目，同样会让安装器
拒装；现在逐 inode 断开，第二份起复制成独立文件。
"""
import os
import stat
import sys
import shutil


def audit(root):
    """Return [(kind, path)] for every link-like entry tar would encode as a
    link. Directory nlink>1 is normal (`.` / `..`), so only regular files."""
    problems = []
    seen = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                problems.append(('symlink', p))
                continue
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    problems.append(('hardlink', p))
                else:
                    seen[key] = p
    return problems


def main(root):
    # realpath both sides: on macOS /tmp -> /private/tmp, and comparing an
    # abspath root against a realpath target would wrongly report OUTSIDE.
    root = os.path.realpath(os.path.abspath(root))

    # 1) 符号链接：自底向上处理避免遍历失真
    links = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                links.append(p)
    n_sym = 0
    for p in sorted(links, key=len, reverse=True):
        tgt = os.path.realpath(p)
        if not os.path.exists(tgt):
            print(f'DANGLING {p} -> {os.readlink(p)}', file=sys.stderr)
            sys.exit(1)
        if not tgt.startswith(root + os.sep):
            print(f'OUTSIDE {p} -> {tgt}', file=sys.stderr)
            sys.exit(1)
        tgt_mode = stat.S_IMODE(os.stat(tgt).st_mode)
        os.unlink(p)
        if os.path.isdir(tgt):
            shutil.copytree(tgt, p, symlinks=False, dirs_exist_ok=False)
        else:
            shutil.copy2(tgt, p)
        os.chmod(p, tgt_mode)
        n_sym += 1

    # 2) 硬链接：同一 inode 在树内的第二份及以后，复制成独立 inode。
    #    os.replace 是目录项级原子替换，原 inode 仍被第一份引用。
    seen = set()
    n_hard = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink < 2:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                mode = stat.S_IMODE(st.st_mode)
                tmp = p + '.delink-tmp'
                shutil.copy2(p, tmp)
                os.replace(tmp, p)
                os.chmod(p, mode)
                n_hard += 1
            else:
                seen.add(key)

    # 3) 校验：软/硬链接都必须清零
    rest = audit(root)
    if rest:
        for kind, p in rest[:20]:
            print(f'REMAINS {kind} {p}', file=sys.stderr)
        sys.exit(1)
    print(f'delinked {n_sym} symlinks + {n_hard} hardlinks under {root}')


if __name__ == '__main__':
    main(sys.argv[1])
