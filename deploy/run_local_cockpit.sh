#!/usr/bin/env bash
set -euo pipefail

# A published loopback port needs a bridged namespace. Allow socket replies and
# the local health probe before handing control to the unprivileged API process.
test "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6)" = 1
iptables -w -P OUTPUT DROP
iptables -w -F OUTPUT
iptables -w -A OUTPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT
iptables -w -A OUTPUT -o lo -d 127.0.0.1/32 -p tcp -m tcp --dport 8000 -j ACCEPT
exec setpriv --reuid 10001 --regid 10001 --clear-groups --bounding-set=-all \
    --inh-caps=-all --ambient-caps=-all --no-new-privs bash deploy/run_daily_shadow.sh "$@"
