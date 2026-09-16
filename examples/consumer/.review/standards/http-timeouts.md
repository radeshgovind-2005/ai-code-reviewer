---
id: http-timeouts
title: Outbound HTTP calls have timeouts
level: SHOULD
status: approved
paths: ["*.py", "*.ts", "*.js", "*.go"]
---
Every outbound HTTP/gRPC call sets an explicit timeout. Library defaults are
often "wait forever", which turns a slow dependency into an outage.
