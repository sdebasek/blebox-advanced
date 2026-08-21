# Changelog

## [0.6.2](https://github.com/sdebasek/blebox-advanced/compare/v0.6.1...v0.6.2) (2026-08-21)


### Bug fixes

* Trim the options dialog and the controls an unused input adds ([#20](https://github.com/sdebasek/blebox-advanced/issues/20)) ([a22694a](https://github.com/sdebasek/blebox-advanced/commit/a22694aa6794f27b2bf459e7a506bdb8b71d1f29))

## [0.6.1](https://github.com/sdebasek/blebox-advanced/compare/v0.6.0...v0.6.1) (2026-08-21)


### Bug fixes

* Correct seven defects found by review ([#17](https://github.com/sdebasek/blebox-advanced/issues/17)) ([c6f3d97](https://github.com/sdebasek/blebox-advanced/commit/c6f3d97c8cad9609e57bdc75081c1915a34ebb08))
* Correct the supported Home Assistant floor and three cost defects ([#18](https://github.com/sdebasek/blebox-advanced/issues/18)) ([2a371a5](https://github.com/sdebasek/blebox-advanced/commit/2a371a5769d3a4904d33d8c248d811907d14550d))
* Make the docs and the code say the same thing ([#19](https://github.com/sdebasek/blebox-advanced/issues/19)) ([2c6775e](https://github.com/sdebasek/blebox-advanced/commit/2c6775eb5b7e9e9c989f64117e681f25c913fd87))


### Documentation

* Refer to the Simon GO range rather than one model of it ([#15](https://github.com/sdebasek/blebox-advanced/issues/15)) ([8e2df59](https://github.com/sdebasek/blebox-advanced/commit/8e2df597b91a2b1099080a5592ed23312f786460))

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
