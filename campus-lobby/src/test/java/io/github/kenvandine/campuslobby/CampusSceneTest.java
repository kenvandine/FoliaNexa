package io.github.kenvandine.campuslobby;

import org.junit.jupiter.api.Test;

import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CampusSceneTest {

    @Test
    void defaultSceneIncludesEveryLandmarksSign() {
        CampusScene.Scene scene = CampusScene.generate(SceneConfig.defaults());

        Set<String> signText = new HashSet<>();
        for (SignPlacement sign : scene.signs()) {
            signText.addAll(sign.lines());
        }

        assertTrue(signText.contains("Memorial Belltower"));
        assertTrue(signText.contains("Howl at the Wolfpack statue"));
        assertTrue(signText.contains("Free Expression Tunnel"));
        assertTrue(signText.contains("Talley Student Union"));
        assertTrue(signText.contains("D. H. Hill Jr. Library"));
        assertEquals(5, scene.signs().size());
    }

    @Test
    void customSignLabelsOverrideDefaults() {
        SceneConfig defaults = SceneConfig.defaults();
        SceneConfig cfg = new SceneConfig(
                defaults.plazaRadius(),
                defaults.towerHeight(),
                defaults.include(),
                defaults.colors(),
                Map.of("belltower", "Custom Tower Name")
        );

        CampusScene.Scene scene = CampusScene.generate(cfg);

        boolean foundCustom = scene.signs().stream()
                .anyMatch(sign -> sign.lines().contains("Custom Tower Name"));
        assertTrue(foundCustom);
    }

    @Test
    void disablingLandmarksOmitsTheirSigns() {
        SceneConfig defaults = SceneConfig.defaults();
        SceneConfig cfg = new SceneConfig(
                defaults.plazaRadius(),
                defaults.towerHeight(),
                new SceneConfig.Include(true, false, false, false, false),
                defaults.colors(),
                Map.of()
        );

        CampusScene.Scene scene = CampusScene.generate(cfg);

        assertEquals(1, scene.signs().size());
        assertTrue(scene.signs().get(0).lines().contains("Memorial Belltower"));
    }

    @Test
    void everyPlacementUsesAValidBukkitMaterialName() {
        CampusScene.Scene scene = CampusScene.generate(SceneConfig.defaults());

        // Bukkit Material enum names are upper snake case; this doesn't
        // require the real org.bukkit.Material enum (kept out of this
        // module's test scope) but does catch typos like lowercase or
        // stray whitespace before they reach the server.
        for (BlockPlacement block : scene.blocks()) {
            assertTrue(block.material().matches("[A-Z0-9_]+"),
                    "not a plausible Material name: " + block.material());
        }
    }

    @Test
    void belltowerIsTallerThanConfiguredHeight() {
        SceneConfig cfg = SceneConfig.defaults();
        CampusScene.Scene scene = CampusScene.generate(cfg);

        int maxY = scene.blocks().stream().mapToInt(BlockPlacement::dy).max().orElseThrow();
        assertTrue(maxY > cfg.towerHeight(), "roof/spire should sit above the configured shaft height");
    }

    @Test
    void wolfpackColorsAreTheDominantPalette() {
        SceneConfig cfg = SceneConfig.defaults();
        CampusScene.Scene scene = CampusScene.generate(cfg);

        Set<String> wolfpackColors = Set.of(
                cfg.colors().primaryRed(), cfg.colors().white(), cfg.colors().black(),
                cfg.colors().brick(), cfg.colors().brickTrim(), cfg.colors().glass(), cfg.colors().roof(),
                "RED_BANNER", "WHITE_BANNER"
        );

        long total = scene.blocks().size();
        long wolfpack = scene.blocks().stream().filter(b -> wolfpackColors.contains(b.material())).count();

        assertTrue(total > 0);
        assertTrue(wolfpack * 100.0 / total > 90.0,
                "expected the Wolfpack/brick palette to dominate, got " + wolfpack + "/" + total);
    }

    @Test
    void emptySceneWhenEveryLandmarkIsDisabledStillHasThePlaza() {
        SceneConfig defaults = SceneConfig.defaults();
        SceneConfig cfg = new SceneConfig(
                defaults.plazaRadius(),
                defaults.towerHeight(),
                new SceneConfig.Include(false, false, false, false, false),
                defaults.colors(),
                Map.of()
        );

        CampusScene.Scene scene = CampusScene.generate(cfg);

        assertFalse(scene.blocks().isEmpty());
        assertEquals(0, scene.signs().size());
    }

    @Test
    void smallerPlazaRadiusStillGeneratesWithoutError() {
        SceneConfig cfg = new SceneConfig(
                16,
                20,
                SceneConfig.defaults().include(),
                SceneConfig.defaults().colors(),
                Map.of()
        );

        CampusScene.Scene scene = CampusScene.generate(cfg);

        assertFalse(scene.blocks().isEmpty());
        List<BlockPlacement> outOfBounds = scene.blocks().stream()
                .filter(b -> Math.abs(b.dx()) > 60 || Math.abs(b.dz()) > 60)
                .toList();
        assertTrue(outOfBounds.isEmpty(), "no landmark should sprawl unreasonably far past the plaza: " + outOfBounds.size());
    }
}
