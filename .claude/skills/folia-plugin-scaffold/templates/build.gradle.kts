// __PLUGIN_NAME__: __ONE_LINE_DESCRIPTION__
//
// Scaffolded by the folia-plugin-scaffold skill (folia-server repo).
// Targets Java 21 / paper-api __PAPER_API_VERSION__, matching this
// cluster's default engine version — see docs/plugin-dev/01-environment-setup.md
// in the folia-server repo for why these exact coordinates.

plugins {
    java
    id("com.gradleup.shadow") version "8.3.5"
}

group = "__GROUP__"
version = "__VERSION__"

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(21))
    }
}

repositories {
    mavenCentral()
    maven("https://repo.papermc.io/repository/maven-public/") // hosts io.papermc.paper:paper-api
}

dependencies {
    compileOnly("io.papermc.paper:paper-api:__PAPER_API_VERSION__")

    testImplementation(platform("org.junit:junit-bom:5.10.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
}

tasks.test {
    useJUnitPlatform()
}

tasks.shadowJar {
    archiveClassifier.set("") // the fat jar IS the plugin jar
}

tasks.build {
    dependsOn(tasks.shadowJar)
}
