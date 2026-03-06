#!/bin/bash
# Entrypoint: fix volume ownership then drop to non-root user
# Docker named volumes are initialized as root — this fixes them on each start.

set -e

# Fix ownership of mounted volumes
chown -R botuser:botuser /app/data /app/logs /home/botuser/.claude 2>/dev/null || true
chown botuser:botuser /shared-media 2>/dev/null || true

# Ensure Claude CLI config exists (prevents warning about missing .claude.json)
if [ ! -f /home/botuser/.claude.json ]; then
    echo '{}' > /home/botuser/.claude.json
    chown botuser:botuser /home/botuser/.claude.json
fi

# Also fix host-mounted files (knowledge, facts, goals)
for f in /app/data/family_knowledge.md /app/data/family_facts.json /app/data/family_goals.json; do
    [ -f "$f" ] && chown botuser:botuser "$f" 2>/dev/null || true
done

# Configure git for self-upgrade commits (inside container)
git config --global user.email "bot@family-bot.local"
git config --global user.name "Family Bot"
git config --global --add safe.directory /host-repo

# SSH setup for git push (deploy key mounted from host)
if [ -f /root/.ssh/id_ed25519 ]; then
    mkdir -p /home/botuser/.ssh
    cp /root/.ssh/id_ed25519 /home/botuser/.ssh/id_ed25519
    cp /root/.ssh/id_ed25519.pub /home/botuser/.ssh/id_ed25519.pub 2>/dev/null || true
    chmod 600 /home/botuser/.ssh/id_ed25519
    # GitHub SSH host keys (avoid ssh-keyscan timing issues)
    cat > /home/botuser/.ssh/known_hosts << 'KEYS'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
KEYS
    # SSH config: map host aliases (github-private, github-upstream) to github.com
    # The host's ~/.ssh/config uses aliases to route deploy keys per-repo.
    # Inside the container, we only have one key, so all aliases → github.com.
    cat > /home/botuser/.ssh/config << 'SSHCONF'
Host github.com github-private github-upstream
    HostName github.com
    IdentityFile ~/.ssh/id_ed25519
    StrictHostKeyChecking accept-new
SSHCONF
    chmod 600 /home/botuser/.ssh/config
    chown -R botuser:botuser /home/botuser/.ssh
fi

# Run the app as botuser
exec gosu botuser python main.py
