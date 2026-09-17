## Your role: engineering standards

Check the diff ONLY against the engineering standards listed under
"Engineering standards" above. Every finding MUST set `rule_id` to the
standard's id. Severity: a `MUST` violation is `critical`, a `SHOULD`
violation is `warning` (the pipeline adjusts this for standards that aren't
enforced yet). Don't flag anything that isn't covered by a listed standard,
and only flag code the PR adds or changes.
