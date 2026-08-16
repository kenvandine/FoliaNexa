package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ServerNameTest {

    @Test
    void qualifyJoinsClusterAndWorld() {
        assertEquals("domain1__world-overworld", ServerName.qualify("domain1", "world-overworld"));
    }

    @Test
    void clusterIdOfExtractsPrefix() {
        assertEquals(Optional.of("domain1"), ServerName.clusterIdOf("domain1__world-overworld"));
    }

    @Test
    void worldNameOfExtractsSuffix() {
        assertEquals(Optional.of("world-overworld"), ServerName.worldNameOf("domain1__world-overworld"));
    }

    @Test
    void worldNameCanItselfContainDoubleUnderscoreAndSplitsOnFirstOccurrence() {
        String qualified = ServerName.qualify("domain1", "world__with__underscores");
        assertEquals(Optional.of("domain1"), ServerName.clusterIdOf(qualified));
        assertEquals(Optional.of("world__with__underscores"), ServerName.worldNameOf(qualified));
    }

    @Test
    void unqualifiedNameYieldsEmpty() {
        assertTrue(ServerName.clusterIdOf("world-overworld").isEmpty());
        assertTrue(ServerName.worldNameOf("world-overworld").isEmpty());
    }
}
