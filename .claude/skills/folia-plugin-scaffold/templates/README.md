# __PLUGIN_NAME__

__ONE_LINE_DESCRIPTION__

__INSPIRATION_NOTE__

## Building

Requires Java 21.

```bash
./gradlew build
# build/libs/__ARTIFACT_ID__-__VERSION__.jar
```

## Installing (for local testing)

Easiest way: from a `folia-server` checkout, run
```bash
tools/folia-nexa-spawn.sh <folia-version> overworld --plugindir=<path to this repo>
```
which builds this plugin and launches a scratch Folia server with it
loaded, printing the address to connect a client to once it's up. See
the [folia-server plugin-dev environment setup guide, §1.8](https://github.com/kenvandine/FoliaNexa/blob/main/docs/plugin-dev/01-environment-setup.md#18-folia-nexa-spawnsh-the-recommended-day-to-day-workflow)
for details, or §1.6/§1.7 there for doing it by hand (drop the built
jar into a Folia/Paper server's `plugins/` directory and (re)start the
server).

## Configuration

`config.yml` is generated on first run. `/__COMMAND_NAME__ reload` picks
up changes without a restart.

## License

__LICENSE__
