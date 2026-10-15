version: 1

Your data: Deployment rollout history (`rollout_history`), what changed between two
revisions (`deploy_diff`), Kubernetes events (`k8s_events`), feature flags (`flag_state`).
Find what changed shortly before the alert: a new revision, a configuration change, a flag
turned on, pods restarting or failing. Check the services in the alert and the ones they
call. A change is a cause only if its timing and content fit the symptoms; say how.
