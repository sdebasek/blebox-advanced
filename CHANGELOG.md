# Changelog

## [0.6.0](https://github.com/sdebasek/blebox-advanced/compare/v0.5.0...v0.6.0) (2026-08-21)


### Features

* Move the relay switch the moment a bound button is pressed ([#12](https://github.com/sdebasek/blebox-advanced/issues/12)) ([79335a1](https://github.com/sdebasek/blebox-advanced/commit/79335a12003be359d2dc4fc6b121157dee0ce67a))


### Bug fixes

* Fall back to /info when a device has no /api/device/state ([9910c06](https://github.com/sdebasek/blebox-advanced/commit/9910c0620a0e65417772195f4cacf89197cd8882))
* Keep the access point switch on the state it was just set to ([fbde612](https://github.com/sdebasek/blebox-advanced/commit/fbde6121c6986671f6ea9f99e9f6936df0bf4b34))
* Stop publishing a relay state the device does not report ([9c09b32](https://github.com/sdebasek/blebox-advanced/commit/9c09b323ae6482e58779ef38233fb82217d62fbf))

## [0.5.0](https://github.com/sdebasek/blebox-advanced/compare/v0.4.2...v0.5.0) (2026-08-21)


### Features

* Add an access point switch and hide unused inputs ([#4](https://github.com/sdebasek/blebox-advanced/issues/4)) ([69fa86c](https://github.com/sdebasek/blebox-advanced/commit/69fa86c2ef82969bf533266a07324e24f975d574))


### Bug fixes

* Remove the vestigial relay state reporting option ([#2](https://github.com/sdebasek/blebox-advanced/issues/2)) ([a307b09](https://github.com/sdebasek/blebox-advanced/commit/a307b09260983291bf63e9fac6ae6e2e6d9fcaf8))
* Stop concurrent device writes corrupting action slots ([#5](https://github.com/sdebasek/blebox-advanced/issues/5)) ([c5cef9f](https://github.com/sdebasek/blebox-advanced/commit/c5cef9fc3420039604b318fae5c266613dbc0980))

## [0.4.2](https://github.com/sdebasek/blebox-advanced/compare/v0.4.1...v0.4.2) (2026-08-20)


### Documentation

* drop the flow diagram from the README ([6070248](https://github.com/sdebasek/blebox-advanced/commit/6070248373235559391e3477a092cbdd29559309))
* explain that the add-integration button needs the install and a restart first ([755fd18](https://github.com/sdebasek/blebox-advanced/commit/755fd1898e1c9eeb79e82b8bf7dd624e96eed695))
* put installation before entities and move event usage into the setup guide ([d60ed21](https://github.com/sdebasek/blebox-advanced/commit/d60ed21471776fe6d323364c2f01cd26f953fa33))
* split setup, troubleshooting and API notes out of the README ([fd963a1](https://github.com/sdebasek/blebox-advanced/commit/fd963a17b5007e684c9f301e88bbb0459c6aa559))
