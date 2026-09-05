# The agent workspace image.
#
# Base must match the host distribution: the agent CLIs are bind-mounted in at
# run time rather than installed here, and they are dynamically linked against
# the host's glibc. Mounting them (instead of baking them in) keeps opencode's
# 177 MB self-updating binary out of the image and lets both CLIs keep updating
# on the host exactly as they do today.
#
# Edit freely — this is your project's toolchain. It is copied to
# ~/.config/multiagents/docker/Dockerfile on first use.
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        ripgrep \
        curl \
        less \
        jq \
        python3 \
        python3-venv \
        build-essential \
 && rm -rf /var/lib/apt/lists/*

# No user is created. The container runs as the invoking user's uid:gid so that
# files an agent writes land with host ownership rather than as root, and HOME
# is supplied per agent at exec time.
WORKDIR /workspace

# Long-lived: one container per project, agents enter it with `docker exec`.
CMD ["sleep", "infinity"]
