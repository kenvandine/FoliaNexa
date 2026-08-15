package io.github.kenvandine.campuslobby;

import java.util.ArrayList;
import java.util.List;

/**
 * Procedurally generates an NC State Wolfpack-themed lobby scene: a
 * central brick plaza (the "Brickyard"), a Belltower centerpiece, a
 * blocky wolf-mascot statue, a Free-Expression-Tunnel-style mural
 * walkway, and stylized student-union / library building facades.
 *
 * This is a deliberately stylized, iconic representation — recognizable
 * silhouettes and Wolfpack colors, not a literal architectural
 * reconstruction (no real building schematics or geometry data were
 * used; see this repo's README). Every shape is generated from plain
 * arithmetic, so it stays unit-testable without a running server.
 *
 * All coordinates are relative to a scene origin at plaza-floor level:
 * +x east, -x west, +z south, -z north, y up from the floor (y=0).
 */
public final class CampusScene {

    private CampusScene() {
    }

    public record Scene(List<BlockPlacement> blocks, List<SignPlacement> signs) {
    }

    public static Scene generate(SceneConfig cfg) {
        Canvas canvas = new Canvas();
        buildPlaza(canvas, cfg);
        if (cfg.include().belltower()) {
            buildBelltower(canvas, cfg);
        }
        if (cfg.include().wolfStatue()) {
            buildWolfStatue(canvas, cfg);
        }
        if (cfg.include().tunnel()) {
            buildTunnel(canvas, cfg);
        }
        if (cfg.include().unionFacade()) {
            buildUnionFacade(canvas, cfg);
        }
        if (cfg.include().libraryFacade()) {
            buildLibraryFacade(canvas, cfg);
        }
        return canvas.toScene();
    }

    // -- The Brickyard: central plaza -----------------------------------

    private static void buildPlaza(Canvas canvas, SceneConfig cfg) {
        int r = cfg.plazaRadius();
        SceneConfig.Colors c = cfg.colors();

        canvas.fillBox(-r, 0, -r, r, 0, r, c.brick());

        // Concentric Wolfpack-colored border rings.
        canvas.ring(-r, -r, r, r, 0, c.black());
        canvas.ring(-(r - 1), -(r - 1), r - 1, r - 1, 0, c.white());
        canvas.ring(-(r - 2), -(r - 2), r - 2, r - 2, 0, c.primaryRed());

        // A white "+" avenue with red pinstripe borders, crossing at the
        // plaza center, connecting the four landmarks.
        int half = 1;
        for (int x = -r; x <= r; x++) {
            for (int dz = -half; dz <= half; dz++) {
                canvas.set(x, 0, dz, c.white());
            }
            canvas.set(x, 0, -half - 1, c.primaryRed());
            canvas.set(x, 0, half + 1, c.primaryRed());
        }
        for (int z = -r; z <= r; z++) {
            for (int dx = -half; dx <= half; dx++) {
                canvas.set(dx, 0, z, c.white());
            }
            canvas.set(-half - 1, 0, z, c.primaryRed());
            canvas.set(half + 1, 0, z, c.primaryRed());
        }

        // A banner pole at each corner, alternating red/white.
        placeFlag(canvas, r - 2, r - 2, c.primaryRed());
        placeFlag(canvas, -(r - 2), r - 2, c.white());
        placeFlag(canvas, r - 2, -(r - 2), c.white());
        placeFlag(canvas, -(r - 2), -(r - 2), c.primaryRed());
    }

    private static void placeFlag(Canvas canvas, int x, int z, String wolfpackColor) {
        String banner = wolfpackColor.startsWith("RED") ? "RED_BANNER" : "WHITE_BANNER";
        canvas.fillBox(x, 1, z, x, 5, z, "OAK_FENCE");
        canvas.set(x, 6, z, banner);
    }

    // -- Memorial Belltower ----------------------------------------------

    private static void buildBelltower(Canvas canvas, SceneConfig cfg) {
        int r = cfg.plazaRadius();
        int height = cfg.towerHeight();
        SceneConfig.Colors c = cfg.colors();

        int halfFoot = 3; // 7x7 footprint
        int cz = -Math.max(halfFoot + 3, r - 8); // north of plaza center

        canvas.fillBox(-halfFoot - 1, 1, cz - halfFoot - 1, halfFoot + 1, 1, cz + halfFoot + 1, c.black());

        // Hollow brick shaft (a wallRing per layer leaves the interior
        // untouched/walkable) with white corner trim pillars.
        for (int y = 2; y <= height; y++) {
            canvas.ring(-halfFoot, cz - halfFoot, halfFoot, cz + halfFoot, y, c.brick());
            canvas.set(-halfFoot, y, cz - halfFoot, c.brickTrim());
            canvas.set(halfFoot, y, cz - halfFoot, c.brickTrim());
            canvas.set(-halfFoot, y, cz + halfFoot, c.brickTrim());
            canvas.set(halfFoot, y, cz + halfFoot, c.brickTrim());
        }

        // Two red accent bands running around the shaft.
        int band1 = 2 + (height - 2) / 3;
        int band2 = 2 + 2 * (height - 2) / 3;
        canvas.ring(-halfFoot, cz - halfFoot, halfFoot, cz + halfFoot, band1, c.primaryRed());
        canvas.ring(-halfFoot, cz - halfFoot, halfFoot, cz + halfFoot, band2, c.primaryRed());

        // A simple white/black clock face on the south wall (facing the plaza).
        int clockY = height - 6;
        canvas.fillBox(-1, clockY - 1, cz + halfFoot, 1, clockY + 1, cz + halfFoot, c.white());
        canvas.set(0, clockY, cz + halfFoot, c.black());
        canvas.set(0, clockY + 1, cz + halfFoot, c.black());

        // Archway entrance carved into the south wall.
        canvas.fillBox(0, 2, cz + halfFoot, 0, 4, cz + halfFoot, "AIR");

        // Stepped black roof, tapering to a spire cap.
        int roofY = height + 1;
        for (int step = 0; step <= halfFoot; step++) {
            int w = halfFoot - step;
            canvas.fillBox(-w, roofY + step, cz - w, w, roofY + step, cz + w, c.roof());
        }
        canvas.set(0, roofY + halfFoot + 1, cz, c.black());

        canvas.sign(0, 1, cz + halfFoot + 2, 0, "NC State Wolfpack", label(cfg, "belltower", "Memorial Belltower"));
    }

    // -- Wolf mascot statue ------------------------------------------------

    private static void buildWolfStatue(Canvas canvas, SceneConfig cfg) {
        int r = cfg.plazaRadius();
        SceneConfig.Colors c = cfg.colors();

        int baseX = Math.max(10, r - 10);
        int baseZ = 6;

        canvas.fillBox(baseX - 2, 1, baseZ - 3, baseX + 2, 1, baseZ + 3, c.black());

        // Four legs.
        canvas.fillBox(baseX - 2, 2, baseZ - 2, baseX - 2, 3, baseZ - 2, c.black());
        canvas.fillBox(baseX + 2, 2, baseZ - 2, baseX + 2, 3, baseZ - 2, c.black());
        canvas.fillBox(baseX - 2, 2, baseZ + 2, baseX - 2, 3, baseZ + 2, c.black());
        canvas.fillBox(baseX + 2, 2, baseZ + 2, baseX + 2, 3, baseZ + 2, c.black());

        // Torso, with a white underbelly stripe.
        canvas.fillBox(baseX - 2, 4, baseZ - 2, baseX + 2, 6, baseZ + 2, c.primaryRed());
        canvas.fillBox(baseX - 2, 4, baseZ - 1, baseX + 2, 4, baseZ + 1, c.white());

        // Tail.
        canvas.fillBox(baseX + 3, 5, baseZ, baseX + 4, 7, baseZ, c.primaryRed());

        // Neck/head extending toward the plaza center (-z), with a white
        // muzzle, black nose, ears and eyes.
        canvas.fillBox(baseX - 1, 6, baseZ - 4, baseX + 1, 8, baseZ - 2, c.primaryRed());
        canvas.fillBox(baseX - 1, 6, baseZ - 5, baseX + 1, 6, baseZ - 4, c.white());
        canvas.set(baseX, 6, baseZ - 5, c.black());
        canvas.set(baseX - 1, 9, baseZ - 3, c.black());
        canvas.set(baseX + 1, 9, baseZ - 3, c.black());
        canvas.set(baseX - 1, 8, baseZ - 3, c.black());
        canvas.set(baseX + 1, 8, baseZ - 3, c.black());

        canvas.sign(baseX, 1, baseZ + 4, 8, "NC State Wolfpack", label(cfg, "wolf-statue", "Howl at the Wolfpack statue"));
    }

    // -- Free Expression Tunnel-style mural walkway -----------------------

    private static void buildTunnel(Canvas canvas, SceneConfig cfg) {
        int r = cfg.plazaRadius();
        SceneConfig.Colors c = cfg.colors();

        int xNear = -Math.max(16, r - 6);
        int xFar = xNear - 6;
        int xMin = Math.min(xNear, xFar);
        int xMax = Math.max(xNear, xFar);
        int zLen = 7;

        for (int z = -zLen; z <= zLen; z++) {
            canvas.fillBox(xMin, 1, z, xMin, 5, z, c.brick());
            canvas.fillBox(xMax, 1, z, xMax, 5, z, c.brick());
        }
        canvas.fillBox(xMin, 6, -zLen, xMax, 6, zLen, c.brick());
        canvas.fillBox(xMin, 0, -zLen, xMax, 0, zLen, c.brick());

        // A colorful mural on both interior walls, biased toward Wolfpack
        // colors with a couple of accent panels for that mural feel.
        String[] accents = {
                c.primaryRed(), c.white(), c.black(),
                c.primaryRed(), c.white(), c.black(),
                "ORANGE_CONCRETE", "LIGHT_BLUE_CONCRETE"
        };
        int i = 0;
        for (int z = -zLen + 1; z <= zLen - 1; z++) {
            String panel = accents[Math.floorMod(i, accents.length)];
            canvas.fillBox(xMin, 2, z, xMin, 4, z, panel);
            canvas.fillBox(xMax, 2, z, xMax, 4, z, panel);
            i++;
        }

        canvas.sign(xMin + (xMax - xMin) / 2, 1, zLen + 1, 0, "NC State Wolfpack", label(cfg, "tunnel", "Free Expression Tunnel"));
    }

    // -- Talley Student Union-style facade --------------------------------

    private static void buildUnionFacade(Canvas canvas, SceneConfig cfg) {
        int r = cfg.plazaRadius();
        SceneConfig.Colors c = cfg.colors();

        int z = Math.max(14, r - 2);
        int halfWidth = 14;
        int height = 11;

        canvas.fillBox(-halfWidth, 1, z, halfWidth, height, z, c.brick());

        for (int x = -halfWidth; x <= halfWidth; x += 4) {
            canvas.fillBox(x, 1, z, x, height, z, c.white());
        }
        for (int x = -halfWidth + 2; x <= halfWidth - 2; x += 4) {
            canvas.fillBox(x, 3, z, x + 1, 5, z, c.glass());
        }
        canvas.fillBox(-halfWidth, height, z, halfWidth, height, z, c.primaryRed());

        canvas.fillBox(-2, 1, z, 2, 4, z, "AIR");
        canvas.fillBox(-2, 5, z, 2, 5, z, c.brickTrim());

        canvas.sign(0, 1, z - 3, 0, "NC State Wolfpack", label(cfg, "union-facade", "Talley Student Union"));
    }

    // -- D. H. Hill Jr. Library-style facade ------------------------------

    private static void buildLibraryFacade(Canvas canvas, SceneConfig cfg) {
        int r = cfg.plazaRadius();
        SceneConfig.Colors c = cfg.colors();

        int x = Math.max(14, r - 2);
        int halfWidth = 14;
        int height = 13;

        canvas.fillBox(x, 1, -halfWidth, x, height, halfWidth, c.brick());

        for (int z = -halfWidth; z <= halfWidth; z += 4) {
            canvas.fillBox(x, 1, z, x, height, z, c.white());
        }
        for (int z = -halfWidth + 2; z <= halfWidth - 2; z += 4) {
            canvas.fillBox(x, 3, z, x, height - 2, z + 1, c.glass());
        }
        canvas.fillBox(x, height, -halfWidth, x, height, halfWidth, c.primaryRed());

        canvas.fillBox(x, 1, -2, x, 4, 2, "AIR");
        canvas.fillBox(x, 5, -2, x, 5, 2, c.brickTrim());

        canvas.sign(x + 3, 1, 0, 12, "NC State Wolfpack", label(cfg, "library-facade", "D. H. Hill Jr. Library"));
    }

    private static String label(SceneConfig cfg, String key, String fallback) {
        String custom = cfg.signLabels().get(key);
        return (custom == null || custom.isBlank()) ? fallback : custom;
    }

    /** Accumulates block/sign placements. Package-private, plain Java. */
    private static final class Canvas {
        private final List<BlockPlacement> blocks = new ArrayList<>();
        private final List<SignPlacement> signs = new ArrayList<>();

        void set(int x, int y, int z, String material) {
            blocks.add(new BlockPlacement(x, y, z, material));
        }

        void fillBox(int x1, int y1, int z1, int x2, int y2, int z2, String material) {
            int xMin = Math.min(x1, x2), xMax = Math.max(x1, x2);
            int yMin = Math.min(y1, y2), yMax = Math.max(y1, y2);
            int zMin = Math.min(z1, z2), zMax = Math.max(z1, z2);
            for (int x = xMin; x <= xMax; x++) {
                for (int y = yMin; y <= yMax; y++) {
                    for (int z = zMin; z <= zMax; z++) {
                        set(x, y, z, material);
                    }
                }
            }
        }

        /** The rectangular outline (perimeter only) of a box at a single height. */
        void ring(int x1, int z1, int x2, int z2, int y, String material) {
            int xMin = Math.min(x1, x2), xMax = Math.max(x1, x2);
            int zMin = Math.min(z1, z2), zMax = Math.max(z1, z2);
            for (int x = xMin; x <= xMax; x++) {
                set(x, y, zMin, material);
                set(x, y, zMax, material);
            }
            for (int z = zMin; z <= zMax; z++) {
                set(xMin, y, z, material);
                set(xMax, y, z, material);
            }
        }

        void sign(int x, int y, int z, int rotation, String... lines) {
            signs.add(new SignPlacement(x, y, z, rotation, List.of(lines)));
        }

        Scene toScene() {
            return new Scene(List.copyOf(blocks), List.copyOf(signs));
        }
    }
}
