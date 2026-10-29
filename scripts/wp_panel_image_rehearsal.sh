#!/bin/bash
# Rehearses the control app's IMAGE on a host that has Docker, from the ThinkPad:   bash scripts/wp_panel_image_rehearsal.sh --host HOST [options]
# It sends app/ to HOST as a build context over ssh (a tar stream: nothing is copied to a file on HOST), builds the image there as wp-panel:rehearsal, runs it the way the real container will run
# (read-only root file system, uid 10001, all capabilities dropped, no new privileges, ONE CPU like aopenpanel has, 384 MB of memory and swap, no network) and lets app/smoke_in_container.sh start the real gunicorn inside it and
# ask it two questions. Then the image is removed, whatever happened. Steps:
#   0. preflight   ssh and sudo -n work, Docker answers, nothing called wp-panel:rehearsal is there already
#   1. build       the recipe (app/Dockerfile) builds from the tar stream; the image runs as uid 10001 and has a health check
#   2. smoke       inside the container: uid, ssh, the packages, a read-only root, a writable state directory, /login answers 200 with the policy, all the workers (3 on one CPU) start, /sites without
#                  a login is sent to /login, no process killed for memory, the memory peak is printed; on a failure the gunicorn log and the kill counter are printed
#   3. cleanup     the image is removed
# It uses no secret, no key and no vault, touches no site, and changes nothing in the repo. Needs only a host with Docker and sudo without a password; pick one that is not dellwp (the build uses its
# CPU and some disk for a minute). Options:  --host NAME (required)  --yes (do not ask before starting)
# What it does not prove: the real secrets and key, the volume mounts and the systemd unit of the real container (the play), HTTPS, the real ssh to dellwp, load. Paste the whole output.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HOST=""; YES=no; TAG=wp-panel:rehearsal
usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case $1 in
    --host) HOST=${2:-}; shift 2 ;;
    --yes) YES=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ $HOST =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || { echo "--host NAME is required: a plain host name, as ssh knows it" >&2; exit 2; }
cd "$REPO" || exit 2
[ -f app/Dockerfile ] && [ -f app/smoke_in_container.sh ] || { echo "app/Dockerfile or app/smoke_in_container.sh is missing" >&2; exit 2; }

FAIL=0; BUILT=no
say() { echo; echo "== $*"; }
bump() { local rc=$1; shift; if [ "$rc" != 0 ]; then echo "FAIL: $* (exit $rc)"; FAIL=$((FAIL+1)); return 1; fi; echo "ok: $*"; return 0; }
yn() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ]; }
remote() { ssh "$HOST" "$@"; }
cleanup() { if [ "$BUILT" = yes ]; then remote "sudo -n docker rmi -f $TAG" </dev/null >/dev/null 2>&1 && echo "   (the image $TAG was removed from $HOST)" || echo "   (could not remove $TAG from $HOST: remove it with: ssh $HOST sudo docker rmi -f $TAG)"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM          # an interrupt ends the script through the EXIT trap, so the image is removed then too
summary() { echo; echo "== summary"; [ "$FAIL" = 0 ] && echo "All checks passed." || echo "$FAIL check(s) failed."; }
fatal() { echo "FAIL: $*"; FAIL=$((FAIL+1)); summary; exit "$FAIL"; }

echo "Control app image rehearsal on $HOST: builds $TAG from app/, runs the smoke test in it, removes it."
[ "$YES" = yes ] || yn "Go on?" || { echo "Nothing was done."; exit 1; }

say "0. preflight"
remote true </dev/null 2>/dev/null; bump $? "ssh to $HOST works" || fatal "cannot ssh to $HOST"
remote 'sudo -n true' </dev/null 2>/dev/null; bump $? "sudo -n works on $HOST" || fatal "sudo -n does not work on $HOST"
V=$(remote 'sudo -n docker version --format "{{.Server.Version}}"' </dev/null 2>/dev/null); RC=$?
bump $RC "Docker answers on $HOST (server $V)" || fatal "Docker does not answer on $HOST"
N=$(remote "sudo -n docker image ls -q $TAG" </dev/null 2>/dev/null | wc -l); [ "$N" = 0 ]; bump $? "no image called $TAG is there already" || fatal "remove it first: ssh $HOST sudo docker rmi -f $TAG"

say "1. build"
tar -C app --exclude=__pycache__ -czf - . | remote "sudo -n docker build -q -t $TAG -" 2>&1 | sed 's/^/   | /'; RC=${PIPESTATUS[1]}
bump "${RC:-1}" "the image builds from the tar stream" || fatal "the build failed: read the lines above"
BUILT=yes
U=$(remote "sudo -n docker image inspect -f '{{.Config.User}}' $TAG" </dev/null 2>/dev/null)
[ "$U" = "10001:10001" ]; bump $? "the image runs as 10001:10001 (it says: $U)"
H=$(remote "sudo -n docker image inspect -f '{{if .Config.Healthcheck}}yes{{else}}no{{end}}' $TAG" </dev/null 2>/dev/null)
[ "$H" = yes ]; bump $? "the image has a health check"
S=$(remote "sudo -n docker image inspect -f '{{.Size}}' $TAG" </dev/null 2>/dev/null)
echo "   image size: $((${S:-0} / 1048576)) MB"

say "2. smoke test inside the container"
remote "sudo -n docker run --rm -i --name wp-panel-rehearsal --read-only --tmpfs /tmp --tmpfs /dev/shm --tmpfs /run/panel:uid=10001,gid=10001,mode=700 --cap-drop ALL --security-opt no-new-privileges --cpuset-cpus 0 --memory 384m --memory-swap 384m --network none $TAG sh -s" < app/smoke_in_container.sh 2>&1 | sed 's/^/   | /'
RC=${PIPESTATUS[0]}
bump "$RC" "the smoke script ended with status $RC"

say "3. cleanup"
cleanup; BUILT=no
summary
echo "   Nothing was committed or edited in the repo. The image is gone from $HOST."
exit "$FAIL"
