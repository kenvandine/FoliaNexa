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

Drop the built jar into a Folia/Paper server's `plugins/` directory and
(re)start the server. See the
[folia-server plugin-dev environment setup guide](https://github.com/kenvandine/FoliaNexa/blob/main/docs/plugin-dev/01-environment-setup.md)
for running a local test server.

## Configuration

`config.yml` is generated on first run. `/__COMMAND_NAME__ reload` picks
up changes without a restart.

## License

__LICENSE__
