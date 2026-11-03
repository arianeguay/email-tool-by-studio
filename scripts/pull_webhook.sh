#!/usr/bin/env bash
# Run the update from the serving host or from a workstation.
#
# `make pull-webhook` was only runnable where the webhook runs: it sudoes to the service
# account, merges into the serving checkout and restarts the unit. From anywhere else the
# operator had to carry the host and the checkout path in their head, and a step carried
# in someone's head is the step that gets skipped.
#
# `WEBHOOK_SSH_HOST` decides which machine this is, and it is the key that already means
# exactly that: `tunnel.reachable` reads it as "the host the webhook serves on, reached
# through ssh" -- set on a workstation, unset on the serving host, where the hourly cron
# already resolves everything locally. Reusing it means neither side configures a second
# thing, and the two sides cannot disagree about which is which.
#
# Unset does not mean "assume this is the serving host". On a workstation that never set
# it, the local leg would merge into this checkout and `pip install` into this venv --
# both of which succeed -- before the restart it cannot do fails. So the local leg asks
# for positive evidence.
#
# The unit file's *presence* is not that evidence, and this workstation is why: it carries
# an `email-classifier-webhook.service` from an install predating the service account
# (STU-1116), enabled, failed, pointing `User=<operator>` at a dev checkout. A test on the
# file alone passes there and merges into the wrong tree. What the unit *says* does
# separate them: a correctly installed serving host runs it as the nologin service
# account, out of the checkout being updated -- both of which `install-webhook-service`
# writes and one of which it already refuses to write otherwise. So the guard reads them
# back, and a stale unit fails it by naming somebody's login account.
#
# The far side runs `pull-webhook-here` and never `pull-webhook`, so the dispatch cannot
# reach itself whatever the far side's environment says.
set -euo pipefail

host=${WEBHOOK_SSH_HOST:-}
checkout=${WEBHOOK_CHECKOUT:-/opt/email-classifier}

if [ -n "$host" ]; then
    # A tty, unlike every other ssh in this repo: those run unattended and must never
    # meet a prompt, this one is an operator deploying and its `sudo systemctl` may have
    # a password to ask for. It also streams the deploy's output as it happens.
    exec ssh -t "$host" "cd '$checkout' && make pull-webhook-here"
fi

unit=/etc/systemd/system/email-classifier-webhook.service
account=${SERVICE_USER:-email-classifier}
here=$(pwd -P)

serves=
if [ -f "$unit" ]; then
    serves=$(sed -n 's/^User=//p' "$unit" | tail -1)
    directory=$(sed -n 's/^WorkingDirectory=//p' "$unit" | tail -1)
fi

if [ "$serves" != "$account" ] || [ "${directory:-}" != "$here" ]; then
    {
        echo "pull-webhook: nothing says which machine to update, and this one does not"
        echo "look like the host that serves the webhook, so there is nothing to pull into."
        echo
        if [ -z "$serves" ]; then
            echo "  $unit is not here."
        else
            echo "  $unit runs as '$serves' out of ${directory:-no directory}, and this"
            echo "  would update '$account' in $here."
        fi
        echo
        echo "  From a workstation: set WEBHOOK_SSH_HOST to the serving host -- the same"
        echo "  key \`make check-webhook\` already uses -- and WEBHOOK_CHECKOUT if its"
        echo "  checkout is not $checkout. Then run this again."
        echo
        echo "  On the serving host: run this from the checkout the unit serves. If it has"
        echo "  never been installed, \`make deploy-webhook\` is what installs it."
    } >&2
    exit 1
fi

exec make pull-webhook-here
