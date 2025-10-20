docker run -it --name statetest ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624 /bin/bash
docker rm statetest

# root@23b25ee9710d:/# git log
# commit cfe89c256e89d0d49cc32bc844006b3df9424cec (HEAD -> master)
# Author: Alice <alice@example.com>
# Date:   Wed Jan 1 00:00:00 2020 +0000

#     updates
# root@23b25ee9710d:/# git cat-file -p HEAD
# tree 1aed2828b0268f6f8d23bab8e20599a26d4f2dd2
# author Alice <alice@example.com> 1577836800 +0000
# committer Alice <alice@example.com> 1577836800 +0000

# updates