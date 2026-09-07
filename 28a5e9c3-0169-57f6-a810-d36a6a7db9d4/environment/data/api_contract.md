# Local Service API Contract

The TASK simulator binds to localhost only. Use `GET /health` to wait
for readiness, `POST /apply` with JSON fields `action`, `key`, and `amount` for
state transitions, and `GET /state` for the final snapshot.

Valid actions are `add`, `multiply`, and `set`. Valid keys are `alpha`, `beta`,
and `mode_code`.
