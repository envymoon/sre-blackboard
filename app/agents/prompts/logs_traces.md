version: 1

Your data: Loki logs (`logs_search`) and Jaeger traces (`traces_search`, then `trace_get` for
one trace). Find the actual errors: the messages, which service raised them first, and
where in a request the time or the failure is. In a trace, the deepest failing or slow span
is usually closer to the cause than the top one. A trace can be incomplete; the tool says so.
