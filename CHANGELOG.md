# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is derived from git tags by `hatch-vcs`: a `vX.Y.Z` tag (created via
a GitHub Release) becomes version `X.Y.Z`. Record changes under `[Unreleased]`
and rename that heading to the version when you cut the release.

## [1.4.0](https://github.com/obeone/caucus-mcp/compare/v1.3.0...v1.4.0) (2026-06-19)


### Added

* **protocol:** forbid harness-blocking tools while in the room ([30a2989](https://github.com/obeone/caucus-mcp/commit/30a29891598e762b9f1bb77622415f255722c077))


### Documentation

* **claude:** drop the mandatory per-change version bump rule ([0a9fb76](https://github.com/obeone/caucus-mcp/commit/0a9fb766e53e78f629d439bb5f6633ab985d2466))
* **claude:** make PROTOCOL_VERSION bump mandatory on any protocol edit ([c080ad2](https://github.com/obeone/caucus-mcp/commit/c080ad2eb7d2774338c11a1b420a736b00c70465))
* document tag-driven versioning ([063641f](https://github.com/obeone/caucus-mcp/commit/063641f103ce7277e2f19b25a572918d60ada432))

## [Unreleased]

### Changed

- **Versioning** — the package version is now derived from git tags via
  `hatch-vcs` instead of a hardcoded `[project].version`. Releases are cut by
  tagging `vX.Y.Z` (a GitHub Release); a new `Release` workflow builds and
  publishes to PyPI on tag push. No more `chore(release): bump version` commits.

## [1.3.0](https://github.com/obeone/caucus-mcp/compare/v1.2.1...v1.3.0) (2026-06-18)


### Added

* add an export button to the operator console ([98541d7](https://github.com/obeone/caucus-mcp/commit/98541d75b487399909d5f121179c6a42bf5b8996))
* add war room hub and MCP bridge package ([2689431](https://github.com/obeone/caucus-mcp/commit/2689431373fbdc97cb4bd45561d56eb7112c5cab))
* **bridge:** add channel tools to the MCP bridge ([6caad74](https://github.com/obeone/caucus-mcp/commit/6caad74dfdbfe387fcbab5660dd7bbf66cf5d500))
* **bridge:** add set_channel_topic tool and surface the join directory ([d6b9a33](https://github.com/obeone/caucus-mcp/commit/d6b9a33b27493a6252c855989777d4051a963971))
* **bridge:** add setup() gate and protocol version handshake ([973d6d8](https://github.com/obeone/caucus-mcp/commit/973d6d83051d2d7ffe46204298876136f34bd0ce))
* **bridge:** deregister server-side on leave ([8105700](https://github.com/obeone/caucus-mcp/commit/81057004a9db725e943dc24e87c91245c9b6c8d7))
* **bridge:** expose talking-stick tools ([3dbc33f](https://github.com/obeone/caucus-mcp/commit/3dbc33f5ad069724d19ff1c05aff04a8aa3058b9))
* **bridge:** make the MCP bridge passive until join ([9e7f656](https://github.com/obeone/caucus-mcp/commit/9e7f656fab27072e2535624ada5d90547ebd6fcd))
* **claude-agent:** add talker/worker types and permission-mode selection ([3d4ed05](https://github.com/obeone/caucus-mcp/commit/3d4ed05145af4e31e37382020df68d577f2bdf7f))
* **claude:** add autonomous Claude connector on the Agent SDK ([eb9f45b](https://github.com/obeone/caucus-mcp/commit/eb9f45b0241fb31475b439f8fc70b721fe0ad38c))
* **claude:** add set_channel_topic tool and inject the channel directory ([31673e5](https://github.com/obeone/caucus-mcp/commit/31673e5a1454a1335f8b79b86dbf188ac6b347e8))
* **claude:** let the native agent open and use private channels ([b8e0dac](https://github.com/obeone/caucus-mcp/commit/b8e0dac377456331b61472042e688de0455d28f2))
* **connector:** add ask_operator/list_forms to bridge and native connector ([d35cbd2](https://github.com/obeone/caucus-mcp/commit/d35cbd2eb2efe3b9be9baa559b72df58b722e14d))
* **connector:** add async HubConnector for native agents ([d1f51ba](https://github.com/obeone/caucus-mcp/commit/d1f51ba373b10df9720789e599b3d09e4da98c62))
* **connector:** expose channel join/leave on the hub connector ([e940bdd](https://github.com/obeone/caucus-mcp/commit/e940bdd3da5320d59e1fa5f93d6e1aa0e6267c2a))
* **connector:** expose set_channel_topic and the registration directory ([dc001fc](https://github.com/obeone/caucus-mcp/commit/dc001fccd39adf373cf1ab33fca251de7b468f53))
* **connector:** resend token on re-join and handle name_in_use ([d8e1067](https://github.com/obeone/caucus-mcp/commit/d8e10676d628e17db1eadbbae184efff30e15e94))
* **connector:** talking-stick on the native path ([a868e7a](https://github.com/obeone/caucus-mcp/commit/a868e7a046a5998e4430c77a234e23efcd2a1545))
* **disklog:** opt-in append-only JSONL event log ([732448e](https://github.com/obeone/caucus-mcp/commit/732448e5b7282d8496a765b373fadb02a2dc17c8))
* export the chat log via a /export endpoint ([4c1f4e3](https://github.com/obeone/caucus-mcp/commit/4c1f4e39cb4531a29e03ea1fb2d4d849bc8066a5))
* expose version via --version flag and /version endpoint ([c6978e2](https://github.com/obeone/caucus-mcp/commit/c6978e29e94f9b9fb703fc688b9d6755eeba5aad))
* **hub:** add message seq numbers and ACK mechanism with replay on reconnect ([9dc443c](https://github.com/obeone/caucus-mcp/commit/9dc443ca6b2bb1d8e11a6159d1f94bf1d5082067))
* **hub:** add peer ping and self-reported status ([d65147e](https://github.com/obeone/caucus-mcp/commit/d65147ea214b58916f5f16bf6cc855adaf06525d))
* **hub:** add per-channel topics and a connect-time channel directory ([d14ffef](https://github.com/obeone/caucus-mcp/commit/d14ffefc6f10a36aa772a165a40bf286280c8f82))
* **hub:** add talking-stick floor control ([e2fc79d](https://github.com/obeone/caucus-mcp/commit/e2fc79d191cc3583b5eb44bbda26a67d9cffd588))
* **hub:** bump protocol to v5 for the one-shot watcher relaunch contract ([cbd1247](https://github.com/obeone/caucus-mcp/commit/cbd1247c1d0812f741bb853143288955e9356264))
* **hub:** dashboard WS protocol, auth/RBAC and static asset serving ([93d62d4](https://github.com/obeone/caucus-mcp/commit/93d62d4839b32bd2d92620d85b2e450b38f8ac1f))
* **hub:** expose /ask and /forms and form answering over /ui ([55456e2](https://github.com/obeone/caucus-mcp/commit/55456e2faac32e8b940034375e228cc1ec279301))
* **hub:** give channels a convener role for coordinated closes ([8d291e3](https://github.com/obeone/caucus-mcp/commit/8d291e37c2272d14e473be65f1e3dd2ff3cd3cd6))
* **hub:** make channels the default for focused pairs ([7768c4c](https://github.com/obeone/caucus-mcp/commit/7768c4c44ca60e3e8c505ee64b4496b548b9b13c))
* **hub:** open operator console in browser on startup ([1d9054b](https://github.com/obeone/caucus-mcp/commit/1d9054b58cc2b14ecd15da20ede67ed15d00f8a9))
* **hub:** reap idle peers and add POST /leave endpoint ([beb5b2c](https://github.com/obeone/caucus-mcp/commit/beb5b2c3595416ca946778cb98b44ce05991a4f1))
* **hub:** refuse duplicate join under a name held by a live peer ([bca30ce](https://github.com/obeone/caucus-mcp/commit/bca30ce9a78b3ce7df7d9a50e3ec553bf71691c5))
* **hub:** revive idle-reaped peers on authenticated use ([635ebfa](https://github.com/obeone/caucus-mcp/commit/635ebfaa875db0ffb0a978c07e02acfe57ee7f4d))
* **hub:** route messages to private channels ([632ee39](https://github.com/obeone/caucus-mcp/commit/632ee398ad86a4c8ee5bbe95b4d5bcb7274571cb))
* **hub:** serve a versioned operating protocol ([b103bd1](https://github.com/obeone/caucus-mcp/commit/b103bd1a3368c37104e0d0c4ff3b4b9c62f9049b))
* **hub:** teach the protocol about resuming work and the no-mailbox rule ([16afd65](https://github.com/obeone/caucus-mcp/commit/16afd65bd8ba5142f904117100641a4282b4649a))
* invite agents to format messages in Markdown ([10cb2b5](https://github.com/obeone/caucus-mcp/commit/10cb2b54c030c79c03483f297df9ddc40838a667))
* **models:** add Field/Form models and answer message kind ([b2e4c81](https://github.com/obeone/caucus-mcp/commit/b2e4c819a9a211d93947fc55df538d7e091c20e5))
* **models:** reserve operator and hub identities and stamp message origin ([d77489c](https://github.com/obeone/caucus-mcp/commit/d77489cfd0ae9de17d7e7f804d71265faf09e137))
* **protocol:** keep watcher alive while awaiting a peer callback ([1fd0a61](https://github.com/obeone/caucus-mcp/commit/1fd0a61cdf22c74e170bdd4ee279da53f42eada9))
* **protocol:** make the shell watcher the default listener ([064ef06](https://github.com/obeone/caucus-mcp/commit/064ef06eccbbc48b4e7d08b2b8ebbf531b90722b))
* **ratelimit:** add read-only available() probe to TokenBucket ([f68d226](https://github.com/obeone/caucus-mcp/commit/f68d226e9a6e75df2edc9c2f1573ae5f262c143b))
* **state:** add operator-form lifecycle ([f77b7b8](https://github.com/obeone/caucus-mcp/commit/f77b7b885e5d64b79ca84bb83bed683ecd03a0fa))
* **state:** deregister and reap idle peers from the roster ([023176e](https://github.com/obeone/caucus-mcp/commit/023176e7fe915ecec425336535e64a6303688a1d))
* **state:** rich peer info, health metrics, per-peer pause, channel close ([a08faef](https://github.com/obeone/caucus-mcp/commit/a08faef836b3a09d3885c979e3d97226936ee472))
* **ui:** add an operator kick button to the peer roster ([a42b2e0](https://github.com/obeone/caucus-mcp/commit/a42b2e0c5be3ab52eb1eedef51acbccd50b88229))
* **ui:** add operator console served by the hub ([ea5831a](https://github.com/obeone/caucus-mcp/commit/ea5831acfeedfa37886ef3bcfe1575d9ac2eb049))
* **ui:** add operator-form wizard to the console ([64b0d02](https://github.com/obeone/caucus-mcp/commit/64b0d02259ff71197fae48a23d8f8015e489938b))
* **ui:** click names to target replies and highlight operator-bound messages ([773949d](https://github.com/obeone/caucus-mcp/commit/773949df7cc665c8177004901e83f8f367ecc5cc))
* **ui:** finish dashboard panels, add Vitest + Playwright suites ([bb76fae](https://github.com/obeone/caucus-mcp/commit/bb76faee5fdba114a2a56108173b63f545d3f55b))
* **ui:** honor allow_other in operator forms ([552aac5](https://github.com/obeone/caucus-mcp/commit/552aac5a8233eaaacbcfaaef19e5a84fc6d83c04))
* **ui:** operator dashboard SPA (Vite + React + TS + Tailwind + shadcn) ([f2a4af6](https://github.com/obeone/caucus-mcp/commit/f2a4af6e0f092fadda9c2519eb438b1a86c21873))
* **ui:** readable operator feed with safe markdown rendering ([b45bf78](https://github.com/obeone/caucus-mcp/commit/b45bf785a425abec9b76a67336b56da4e04bfc4c))
* **ui:** rename console to Caucus, show hub version, add composer autocomplete ([0ea61bb](https://github.com/obeone/caucus-mcp/commit/0ea61bb3904cfbe68e425aa80c9a0a35d7624301))
* **ui:** show active talking sticks in the operator console ([685e29e](https://github.com/obeone/caucus-mcp/commit/685e29e90bc98b009f2f0083bc2d5bd7eb432c6c))
* **ui:** show channel topics and load web fonts without blocking onload ([9c742f8](https://github.com/obeone/caucus-mcp/commit/9c742f8d14c66cba6ead76658e40076684f4f9d6))
* **ui:** stick-to-bottom auto-scroll in Flow timeline ([d21442a](https://github.com/obeone/caucus-mcp/commit/d21442a15aa99fc3be781b0dce0fcb6b89776302))
* **ui:** surface private channels in the operator console ([1d53f50](https://github.com/obeone/caucus-mcp/commit/1d53f5056572f714797897b7b76da29245ec5706))
* **ui:** v2 dashboard — left-rail layout, composer autocomplete, markdown flow ([cb7dc50](https://github.com/obeone/caucus-mcp/commit/cb7dc50b1ffe1fc0fb4b55b7395e4d8a733e9da6))
* **urlguard:** fail-closed validation for the configurable hub URL ([ebcb10b](https://github.com/obeone/caucus-mcp/commit/ebcb10b7cfbe343b72b0561bbe313c9999e12faf))
* **watch:** add zero-token background watcher ([d726eaf](https://github.com/obeone/caucus-mcp/commit/d726eaf69e878339485d11b170b665d5290619a8))


### Fixed

* **agent:** retry transient hub errors with backoff and guard the hub URL ([9cae76a](https://github.com/obeone/caucus-mcp/commit/9cae76a2f1340b14afbb786476b00514b29c7e6a))
* **agent:** treat inbound peer messages as untrusted to block prompt injection ([c7f599f](https://github.com/obeone/caucus-mcp/commit/c7f599ff1f09ff62f75820497fddbd2de1dec999))
* **bridge:** harden watcher token file, guard hub URL, survive hub blips ([c8c532e](https://github.com/obeone/caucus-mcp/commit/c8c532ebc49175152a045760c71ea2f1ac76de92))
* **bridge:** tell the agent to relay then relaunch the one-shot watcher ([3b8ccc7](https://github.com/obeone/caucus-mcp/commit/3b8ccc79c05ef40678d0654c512df8cf25602e1a))
* **deps:** require claude-agent-sdk &gt;=0.2.93 for the auto permission mode ([db780f0](https://github.com/obeone/caucus-mcp/commit/db780f047f664abec1d2582595b888bbd469f111))
* **disklog:** write the pruned log atomically and serialize with appends ([975c3c1](https://github.com/obeone/caucus-mcp/commit/975c3c1d0740b00cbd253acd0dd6ddd4b03260f8))
* **hub:** bound channel names and rate-limit membership endpoints ([13ea571](https://github.com/obeone/caucus-mcp/commit/13ea57125a498b8b8f10646385c365a821e82377))
* **hub:** deliver broadcast and channel messages to reaped peers ([febb9b8](https://github.com/obeone/caucus-mcp/commit/febb9b818a0daf906f668c9a04f739c971abdb58))
* **hub:** evict same-name reaped ghost on fresh re-register ([d9cb28b](https://github.com/obeone/caucus-mcp/commit/d9cb28b98bfb9e2b61cbffe724b755ecd575888a))
* **hub:** gate ui origin, authenticate control, and enforce resource caps ([31a74b7](https://github.com/obeone/caucus-mcp/commit/31a74b7cd891904ca7abd4d8d440eb70a117fd22))
* **hub:** limit request body size, gate /export, add console CSP ([e0293b9](https://github.com/obeone/caucus-mcp/commit/e0293b96dc153e55377aafeaa2c3efd832690df9))
* **hub:** raise default client TTL to 300s ([bf0285b](https://github.com/obeone/caucus-mcp/commit/bf0285ba198248a798027cccbce65e8b594f5813))
* **hub:** read /receive token from Authorization header, not URL query ([80071ab](https://github.com/obeone/caucus-mcp/commit/80071ab9ee6e0a76704320433454a17392b48f87))
* **hub:** route direct messages to reaped clients within grace window ([51481ae](https://github.com/obeone/caucus-mcp/commit/51481ae9bbc323bfc360f9ab1d85b7c51a2a8935))
* **hub:** type /send return as SendResponse | JSONResponse ([f5665c8](https://github.com/obeone/caucus-mcp/commit/f5665c80c42b5441d289fc888550073bb5c61c31))
* **logging:** silence httpx request logging to stop token leak ([46c0eaa](https://github.com/obeone/caucus-mcp/commit/46c0eaae49b52874fce8327627ae6150eae34480))
* **state:** cap in-memory resources and mark hub message provenance ([2356523](https://github.com/obeone/caucus-mcp/commit/235652363bb9fc803087253ca2e81f94deb1a5de))
* **ui:** clarify required-Other validation message in form wizard ([76fce99](https://github.com/obeone/caucus-mcp/commit/76fce99ccea9e939513b90bf61711eb0e4981fd4))
* **ui:** keep the operator composer pinned to the viewport bottom ([884772a](https://github.com/obeone/caucus-mcp/commit/884772acf06db829315022994bf44e5a53794e5b))
* **ui:** preserve scroll position when reading scrollback ([e178ddb](https://github.com/obeone/caucus-mcp/commit/e178ddb23c2fe7bc96b63c19b5cafbacda4700df))
* **ui:** unpack nested hub message event (Flow panel crash) ([df44316](https://github.com/obeone/caucus-mcp/commit/df44316d26a349d9865cebab753466e79c885858))
* **watch:** exit one-shot-per-wake so inbound messages reach the agent ([90c72be](https://github.com/obeone/caucus-mcp/commit/90c72be194a9458277d9fd579575ab140b485e18))
* **watch:** guard the hub URL and tolerate malformed hub responses ([c00e92c](https://github.com/obeone/caucus-mcp/commit/c00e92cd95d27d8fea8e9e4dbb6aecfa1d50934e))


### Documentation

* add CHANGELOG and CONTRIBUTING ([31ab6b6](https://github.com/obeone/caucus-mcp/commit/31ab6b6061983030164775114a3d659dbc1ac5c4))
* add peer war room operating protocol ([5fb816d](https://github.com/obeone/caucus-mcp/commit/5fb816d44dab5b4676c52b28b2c28726306d049b))
* add README and Claude Code guidance ([11614f4](https://github.com/obeone/caucus-mcp/commit/11614f4d738c3e1fd6a9e900e415209a7ff4e8aa))
* assume public repo and PyPI distribution in install steps ([e3026da](https://github.com/obeone/caucus-mcp/commit/e3026dab757796cc0d6d1aab06350385c293808e))
* **changelog:** document 1.1.0, 1.2.0, and 1.2.1 releases ([12f9098](https://github.com/obeone/caucus-mcp/commit/12f9098d873edeccb122977cb911efbb91ed830b))
* **dashboard:** freeze operator dashboard WS protocol contract ([547722c](https://github.com/obeone/caucus-mcp/commit/547722c7fc5e2bf6a3d067b4f7af7d597adb5ce0))
* **dashboard:** operator runbook, architecture and README ([59abd40](https://github.com/obeone/caucus-mcp/commit/59abd40e49f9884854636a6859884e2d00d7db02))
* document duplicate-join protection and operator kick ([898ee20](https://github.com/obeone/caucus-mcp/commit/898ee2077189778f0ad8b6c4e2ac4f5f880147b5))
* document operator forms ([87f83ec](https://github.com/obeone/caucus-mcp/commit/87f83ec53168e1531313c87a543b94958ee76997))
* document peer ping and status tools ([169b7d2](https://github.com/obeone/caucus-mcp/commit/169b7d28eb88f39d1c04b7ff7de39b841f81e9c5))
* document peer reaping and the /leave endpoint ([4652a5a](https://github.com/obeone/caucus-mcp/commit/4652a5a9ece0c17f2b32bd9a8a88e223cb21c66d))
* document private channels in the project guide ([7973b32](https://github.com/obeone/caucus-mcp/commit/7973b321e9a1e06a345720659875fae7c24f6311))
* document reaped-peer revival and the 300s TTL ([cfe7b2c](https://github.com/obeone/caucus-mcp/commit/cfe7b2c84278c60011a520f8d7e58c5a665a503d))
* document setup() and the hub-served protocol ([b9daef0](https://github.com/obeone/caucus-mcp/commit/b9daef06732d2accd8aa0dcea4509862fc5cf106))
* document talking-stick floor control ([2d9641d](https://github.com/obeone/caucus-mcp/commit/2d9641d5915d482a642c608995814768b061666a))
* document the one-shot-per-wake watcher contract ([b2103e8](https://github.com/obeone/caucus-mcp/commit/b2103e877e1e1a835f1a8c6e55edee4234f72745))
* document the passive bridge and join/leave loop ([c420de1](https://github.com/obeone/caucus-mcp/commit/c420de1f988ed18734ba99aeeaffb97175165727))
* document watcher-on-join lifecycle and communicative style ([53a6bbd](https://github.com/obeone/caucus-mcp/commit/53a6bbdde55ef58886065e5dbaf1911001dc9ab8))
* drop Claude-specific framing, position hub as MCP-client-agnostic ([86267ea](https://github.com/obeone/caucus-mcp/commit/86267ea383622e3d79d7e9cf343e50b2e2b8642f))
* extract architecture detail into docs/ARCHITECTURE.md ([3d7ed9e](https://github.com/obeone/caucus-mcp/commit/3d7ed9e55c7f66d3dd0ce8f889fc19a001a9614b))
* include MCP client config in the quickstart ([f468ec5](https://github.com/obeone/caucus-mcp/commit/f468ec598ec0412cd45e495074bf4b146f341178))
* note the /export endpoint in the architecture overview ([e86af28](https://github.com/obeone/caucus-mcp/commit/e86af289057613a690122fe8af010d0f1da34f51))
* offer pipx and pip alternatives in the quickstart ([cc7176c](https://github.com/obeone/caucus-mcp/commit/cc7176c36c2963712f9d991797d65dfc3e763e1d))
* point CLAUDE.md at the new pytest suite ([b4a6853](https://github.com/obeone/caucus-mcp/commit/b4a68533fbd13c8457bc1474a9e7a9c6b4d89a27))
* **readme:** mark 1.0 stable, add license badge, document talker/worker profiles ([8ec7701](https://github.com/obeone/caucus-mcp/commit/8ec77012d483515f42f166a4c4cf7bfc4eb91519))
* **readme:** restructure and refresh the project overview ([1064f4f](https://github.com/obeone/caucus-mcp/commit/1064f4f5d384754857b5deff24173be92f00a486))
* reframe architecture around layered connectors ([124d2c7](https://github.com/obeone/caucus-mcp/commit/124d2c7d3311dcced113a41e8da544af9a15c993))
* reorder README — use cases before quickstart, architecture before development ([b02207a](https://github.com/obeone/caucus-mcp/commit/b02207a27c6fc7a56f3bf6ed9f646c6e88144a1c))
* require a version bump on every release ([7deb7a4](https://github.com/obeone/caucus-mcp/commit/7deb7a49f901c3e71604426296e615380d073191))
* rewrite README with badges, diagrams, use cases, and install paths ([182b3c1](https://github.com/obeone/caucus-mcp/commit/182b3c12fb87ac6e86becd3e3007658a8dd0a7ad))
* sharpen cross-repo use case around ownership boundaries ([80b3244](https://github.com/obeone/caucus-mcp/commit/80b3244cad1791eccf5b26c0f6635ee9692508d6))
* slim CLAUDE.md to overview and invariants, link architecture doc ([463d167](https://github.com/obeone/caucus-mcp/commit/463d1679d96bf9d3e490770f06b28622d5bd0b55))


### Changed

* rename project from War Room to Caucus ([af2c7c1](https://github.com/obeone/caucus-mcp/commit/af2c7c1fe48df8cd5846234e9f3fa471791f5560))
* ship operator UI as package data ([011e91a](https://github.com/obeone/caucus-mcp/commit/011e91a0e1309b8330708ceb81535dba94a419ff))
* single-source the package version from pyproject.toml ([c88c9ce](https://github.com/obeone/caucus-mcp/commit/c88c9ce3ccb9d70a7119e5cbf7c38cc901365ed1))
* **ui:** drop dead channel branch in recipient rendering ([1af200d](https://github.com/obeone/caucus-mcp/commit/1af200d6154b603a830167b86292a0af760de5aa))

## [Unreleased]

## [1.2.1] — 2026-06-18

### Security

- **Dependencies** — refreshed the lockfile to pull patched versions
  addressing upstream security advisories.
- **CI** — restricted the workflow `GITHUB_TOKEN` to read-only
  (`contents: read`).

## [1.2.0] — 2026-06-18

Second hardening pass, focused on the configurable hub URL and resilience.

### Security

- **URL guard** — fail-closed validation for the operator-configurable hub
  URL, shared across every connector.
- **Bridge / watcher / agent** — guard the hub URL, harden the watcher token
  file, tolerate malformed hub responses, and survive transient hub blips with
  bounded retry/backoff.
- **Hub** — limit request body size, gate `/export`, and add a console CSP.
- **Disk log** — write the pruned event log atomically and serialize it with
  appends to avoid corruption.
- Regression tests covering the Low-severity hardening items.

## [1.1.0] — 2026-06-18

First security hardening pass after the stable release.

### Security

- **Prompt-injection containment** — inbound peer messages are treated as
  untrusted by the native agent.
- **Identity & provenance** — reserve the operator and hub identities and stamp
  every message with its origin.
- **Resource caps** — cap in-memory resources, gate the UI origin (anti-CSWSH),
  authenticate the `/control` channel, and enforce throughput caps.
- **Rate limit** — read-only `available()` probe on the token bucket.
- Test suite covering auth, CSWSH, caps, throttle, and provenance.

## [1.0.0] — 2026-06-17

First stable release. The protocol, HTTP API, and CLI surface are now
considered stable under SemVer.

### Highlights

- **Supervised multi-agent hub** — a FastAPI process where agents talk
  directly, by broadcast, or in private `#`-channels, all under a human
  operator who watches live and can pause, stop, reset, or kick.
- **Two connectors over one hub** — a passive `caucus-bridge` (with the
  zero-token `caucus-watch` listener) for turn-based MCP hosts, and a native
  autonomous `caucus-claude-agent` on the Claude Agent SDK that owns its loop.
- **Hub-owned operating protocol** — served versioned at `/protocol`; clients
  fetch it at `setup()` and re-read it when `PROTOCOL_VERSION` moves.
- **Talking stick** floor control — any peer can seize a lane so a grave
  message is heard; the operator can clear it.
- **Private channels** with topics and a connect-time directory; convener role
  for coordinated closes.
- **Operator forms** — an agent pushes a questionnaire, the operator answers
  once in a console wizard, and the bundle routes back as an `answer` message.
- **Agent profiles** — `talker` (caucus tools only) vs `worker` (also wields
  built-in Claude Code tools), with a selectable permission mode.
- **Operator dashboard SPA** (Vite + React + TS + Tailwind + shadcn) served by
  the hub, with Health / Flow / Channels / Forms panels over the `/ui`
  WebSocket; optional operator/observer token auth and RBAC.
- **Loop safety** — per-sender token-bucket rate limiting and a hard operator
  Stop every agent observes; an idle reaper drops quiet peers.
- **Observability** — message sequence numbers with ACK and replay on
  reconnect, an opt-in append-only JSONL event log, and a `/export` endpoint.

## Pre-1.0 history

The 0.1 → 0.20 series built the project up in these milestones (see the git
history for per-commit detail):

- **0.1–0.3 — Foundations.** War-room hub + MCP bridge package, operator
  console served by the hub, passive-until-`join` bridge, and a versioned
  operating protocol with a `setup()` gate and version handshake.
- **0.4–0.6 — Listening model.** Zero-token background `caucus-watch` listener
  made the default, idle-peer reaping with `POST /leave`, and the one-shot
  watcher-relaunch contract.
- **0.7–0.9 — Native path & channels.** Async `HubConnector` and the
  autonomous Claude connector on the Agent SDK; private channels with routing,
  per-channel topics, and a connect-time directory; Markdown messages and a
  `/export` endpoint.
- **0.10–0.12 — Roster & resilience.** Duplicate-join protection, token resend
  on re-join, idle-reaped peer revival, ping/status, operator kick, ACK +
  replay on reconnect, agent `talker`/`worker` types and the channel convener.
- **0.13–0.16 — Talking stick & forms.** Floor control across hub, bridge,
  native connector, and console; the operator-form lifecycle end to end;
  `--version` flag and `/version` endpoint.
- **0.17–0.20 — Dashboard & hardening.** The v2 operator dashboard SPA, the
  dashboard WebSocket protocol with auth/RBAC and static asset serving, richer
  peer/health state with per-peer pause, and an opt-in JSONL event log.

[Unreleased]: https://github.com/obeone/caucus-mcp/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/obeone/caucus-mcp/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/obeone/caucus-mcp/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/obeone/caucus-mcp/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/obeone/caucus-mcp/releases/tag/v1.0.0
