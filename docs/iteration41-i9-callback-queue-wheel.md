# Iteration 41 I9 CTP callback queue wheel candidate

This candidate labels the native CTP callback-source and queue-consumer-lease
API from source commit `232a14dc6e055523a8bb5968238eb0e93126ff94` as
`bt_api_ctp` version `2.0.4+iteration41.i9`. The earlier callback-source
commit `69098921025ceaba57ca4c7cdb660e97bdf94217` is an ancestor. The local
version satisfies consumers requiring `bt_api_ctp>=2.0.3,<3.0` and separates
this candidate from the earlier `2.0.3+iteration41.i8` artifact.

There is no callback API or native behavior change in this version-only
candidate. Before any adoption, bind the exact source commit to the reproducible
wheel SHA-256 and its verified `RECORD`; this note does not add a provider
session, default runtime route, or permission to access credentials or submit
orders.