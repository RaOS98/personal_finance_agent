"""HTTP API surface for the personal finance agent.

Currently exposes a single read-only endpoint, ``GET /widget/summary``, used
by the iPhone Scriptable widget. Designed so a future static web dashboard
on S3 + CloudFront can plug into the same backend without breaking changes:

- versioned JSON envelope (``version`` field at the top level)
- CORS headers and ``OPTIONS`` preflight handled by the same Lambda
- read-only IAM scope on the underlying function

The handler in :mod:`api.widget_handler` is the Lambda entrypoint; the
aggregation logic lives in :mod:`api.aggregator` so it can be unit-tested
without spinning up the whole API Gateway event shape.
"""
