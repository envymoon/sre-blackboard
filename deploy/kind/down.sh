#!/usr/bin/env bash
# Delete the fault lab cluster. Nothing in it is persistent.
set -euo pipefail
kind delete cluster --name sre-lab
