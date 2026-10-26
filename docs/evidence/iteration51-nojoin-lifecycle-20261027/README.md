# Iteration 51 CTP nonblocking shutdown source round

`MdClient.start` and `TraderClient.start` accept the explicit keyword `join_observer=False` with `block=False`. The default retains the prior Join observer. The new path waits for startup, request and callback leases, detaches the SPI, releases the exact API once, and waits for its deferred owner to clear before returning a complete stop receipt. Uncertain release and cleanup-worker failures retain an incomplete result.

Source acceptance: 72 default regression passes and one optional missing-module skip; 37 distinct focused cases passed across separate runs, with two affected cases rerun after an unused test local was removed. Root verified the prior test bytes by reconstruction. Independent review is static only. Original console/JUnit and diagnostic failure outputs are preserved here; no accepted suite was repeated for delivery.

This round accepts the base-client source/fake implementation. The separate existing-wheel native primitive test supports Init/detach/Release ordering only. Installed candidate, example integration and account acceptance remain outstanding. `OneShotMdDiagnosticClient` does not expose the option. See `round-record.json` for exact pins, scope and evidence.
