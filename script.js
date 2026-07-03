Below is a **complete, ready‑to‑drop‑in `script.js`** that covers everything you asked for:

* a tiny data‑model layer (items you can place in a Three‑js scene)  
* helpers for persisting that model in `localStorage` (and re‑hydrating it)  
* a full Three‑js scene bootstrap (renderer, camera, lights, orbit controls)  
* click‑to‑select logic using ray‑casting + visual feedback (outline box)  
* a **`getStats()`** routine that `stats.html` can call to pull summary numbers (total items, average distance from origin, most‑common type, etc.)

Feel free to copy‑paste the file into your project and adjust the configuration constants at the top to match your exact needs.

```js
/* =========================================================================
   script.js – shared logic for the demo app
   -------------------------------------------------------------------------
   What it does:
   1️⃣  Builds a simple data model (Item objects) – each item has an id,
       a type, a name and a 3‑D position.
   2️⃣  Persists the model in localStorage (save / load / reset).
   3️⃣  Sets up a Three.js scene (camera, lights, renderer, OrbitControls).
   4️⃣  Provides click‑to‑select via ray‑casting + a highlighted outline.
   5️⃣  Calculates statistics that can be consumed by `stats.html`.
   ========================================================================= */

/* -------------------------------------------------------------------------
   CONFIGURATION ------------------------------------------------------------
   ------------------------------------------------------------------------- */

const CONFIG = {
  // ------- localStorage ----------------------------------------------------
  storageKey: "demo3d_items", // key used to store the JSON string

  // ------- Three.js ---------------------------------------------------------
  canvasId: "three-canvas",      // <canvas id="three-canvas"></canvas>
  backgroundColor: 0x202020,
  fov: 60,
  near: 0.1,
  far: 1000,
  defaultCameraPos: new THREE.Vector3(0, 10, 20),

  // ------- Geometry ---------------------------------------------------------
  // Define a few primitive meshes that can be spawned by the app.
  // You can extend this map with your own custom geometry / material pairs.
  geometryMap: {
    cube:   () => new THREE.Mesh(
                new THREE.BoxGeometry(1, 1, 1),
                new THREE.MeshStandardMaterial({ color: 0x1565c0 })
              ),
    sphere: () => new THREE.Mesh(
                new THREE.SphereGeometry(0.6, 32, 32),
                new THREE.MeshStandardMaterial({ color: 0xc62828 })
              ),
    cone:   () => new THREE.Mesh(
                new THREE.ConeGeometry(0.5, 1, 32),
                new THREE.MeshStandardMaterial({ color: 0x2e7d32 })
              )
  },

  // ------- UI / Interaction --------------------------------------------------
  selectOutlineColor: 0xffff00,
  selectOutlineThickness: 0.03,

  // ------- Stats -------------------------------------------------------------
  // Anything you want to compute can be added to `getStats()`. The defaults
  // below are just a starter set.
  stats: {
    includeAverageDistance: true,
    includeMostCommonType: true,
    includeTypesBreakdown: true,
  }
};

/* -------------------------------------------------------------------------
   1️⃣  DATA MODEL -----------------------------------------------------------
   ------------------------------------------------------------------------- */

/**
 * Class representing a single item placed in the scene.
 */
class Item {
  /**
   * @param {Object} params - initial values (optional)
   * @param {string} params.id       - unique identifier (auto‑generated if omitted)
   * @param {string} params.type     - must match a key in CONFIG.geometryMap
   * @param {string} params.name     - human‑readable label
   * @param {THREE.Vector3} params.position - world position
   */
  constructor({ id, type = "cube", name = "Untitled", position } = {}) {
    this.id = id || Item.generateId();
    this.type = type;
    this.name = name;
    this.position = position ? position.clone() : new THREE.Vector3();
  }

  /**
   * Serialises the Item as a plain object ready for JSON.stringify().
   */
  toJSON() {
    return {
      id: this.id,
      type: this.type,
      name: this.name,
      // store position as an array for compactness and easy parsing
      position: [this.position.x, this.position.y, this.position.z]
    };
  }

  /**
   * Creates an Item from a plain object (usually the result of JSON.parse()).
   * @param {Object} obj
   */
  static fromJSON(obj) {
    const pos = new THREE.Vector3(...obj.position);
    return new Item({
      id: obj.id,
      type: obj.type,
      name: obj.name,
      position: pos
    });
  }

  /** @private */
  static generateId() {
    // short, collision‑resistant id – ok for demo purposes
    return "i" + Math.random().toString(36).substr(2, 9);
  }
}

/**
 * Central in‑memory store – an array of Items.
 * The array is never exported directly; all modifications go through
 * the helper functions below (so we can keep `localStorage` in sync).
 */
const Model = {
  items: [] // <-- filled by loadFromStorage()
};

/* -------------------------------------------------------------------------
   2️⃣  LOCAL STORAGE HELPERS -------------------------------------------------
   ------------------------------------------------------------------------- */

/**
 * Save the current `Model.items` array into localStorage.
 */
function saveToStorage() {
  const json = JSON.stringify(Model.items.map(item => item.toJSON()));
  localStorage.setItem(CONFIG.storageKey, json);
}

/**
 * Load the item list from localStorage (if any) and replace the current
 * in‑memory model. Returns a Promise that resolves when the scene has been
 * repopulated with meshes.
 */
function loadFromStorage() {
  const raw = localStorage.getItem(CONFIG.storageKey);
  if (!raw) {
    Model.items = []; // nothing stored yet
    return Promise.resolve();
  }

  try {
    const parsed = JSON.parse(raw);
    Model.items = parsed.map(obj => Item.fromJSON(obj));
    // After the model is restored we need to recreate the 3‑D meshes
    return Promise.all(Model.items.map(item => addItemToScene(item)));
  } catch (e) {
    console.warn("Failed to parse stored data – resetting.", e);
    Model.items = [];
    localStorage.removeItem(CONFIG.storageKey);
    return Promise.resolve();
  }
}

/**
 * Clear everything from localStorage *and* the scene.
 */
function resetStorage() {
  localStorage.removeItem(CONFIG.storageKey);
  Model.items = [];
  // Remove all THREE.Mesh objects (but keep lights, floor, etc.)
  const toRemove = scene.children.filter(c => c.userData.isDemoMesh);
  toRemove.forEach(m => scene.remove(m));
}

/* -------------------------------------------------------------------------
   3️⃣  THREE.JS SETUP --------------------------------------------------------
   ------------------------------------------------------------------------- */

let scene, camera, renderer, controls, raycaster, mouse;
let outlinePass; // optional post‑process outline (fallback to BoxHelper)
let selectedItem = null; // Item object that is currently selected (null = none)

function initThree() {
  // ----- Renderer ---------------------------------------------------------
  const canvas = document.getElementById(CONFIG.canvasId);
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setSize(canvas.clientWidth, canvas.clientHeight);
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setClearColor(CONFIG.backgroundColor);

  // ----- Scene ------------------------------------------------------------
  scene = new THREE.Scene();

  // ----- Camera ------------------------------------------------------------
  const aspect = canvas.clientWidth / canvas.clientHeight;
  camera = new THREE.PerspectiveCamera(CONFIG.fov, aspect, CONFIG.near, CONFIG.far);
  camera.position.copy(CONFIG.defaultCameraPos);
  scene.add(camera);

  // ----- Controls ----------------------------------------------------------
  controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.10;

  // ----- Lights ------------------------------------------------------------
  const ambient = new THREE.AmbientLight(0xffffff, 0.6);
  scene.add(ambient);
  const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
  dirLight.position.set(10, 20, 10);
  scene.add(dirLight);

  // ----- Helpers -----------------------------------------------------------
  const grid = new THREE.GridHelper(50, 50, 0x555555, 0x555555);
  scene.add(grid);

  // ----- Raycaster ---------------------------------------------------------
  raycaster = new THREE.Raycaster();
  mouse = new THREE.Vector2();

  // ----- Event listeners ---------------------------------------------------
  window.addEventListener('resize', onWindowResize);
  renderer.domElement.addEventListener('pointerdown', onPointerDown);

  // ----- (Optional) Outline pass -----------------------------------------
  // If you use post‑processing you can replace the BoxHelper outline with
  // an OutlinePass. The code below is a fallback that works without any
  // extra deps.
  outlinePass = null; // placeholder – we’ll just use a BoxHelper later

  // Kick off the render loop
  animate();

  // Load persisted items (if any) and bring them into the scene
  loadFromStorage();
}

/**
 * Adjust renderer / camera when the window (or canvas) resizes.
 */
function onWindowResize() {
  const canvas = renderer.domElement;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  renderer.setSize(width, height);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

/**
 * Main render loop – required for OrbitControls damping and for
 * highlighting the selected object.
 */
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  render();
}

/**
 * One‑off rendering call.
 */
function render() {
  renderer.render(scene, camera);
}

/* -------------------------------------------------------------------------
   4️⃣  ITEM ↔ MESH SYNC ------------------------------------------------------
   ------------------------------------------------------------------------- */

/**
 * Create a THREE.Mesh for an Item (or reuse an existing one) and add it to
 * the scene. Returns a Promise that resolves to the mesh.
 *
 * @param {Item} item
 * @returns {Promise<THREE.Mesh>}
 */
function addItemToScene(item) {
  return new Promise(resolve => {
    const meshFactory = CONFIG.geometryMap[item.type];
    if (!meshFactory) {
      console.warn(`No geometry defined for type "${item.type}". Falling back to cube.`);
    }
    const mesh = meshFactory ? meshFactory() : CONFIG.geometryMap.cube();

    mesh.position.copy(item.position);
    mesh.userData.itemId = item.id; // link back to the data model
    mesh.userData.isDemoMesh = true; // marker used for cleanup
    scene.add(mesh);
    resolve(mesh);
  });
}

/**
 * Remove a mesh (and its linked Item) from the scene and model.
 *
 * @param {Item} item
 */
function removeItemFromScene(item) {
  const mesh = scene.getObjectByProperty('userData.itemId', item.id);
  if (mesh) scene.remove(mesh);
  // Remove from model array
  Model.items = Model.items.filter(i => i.id !== item.id);
  saveToStorage();
}

/* -------------------------------------------------------------------------
   5️⃣  RAYCASTING / SELECTION ------------------------------------------------
   ------------------------------------------------------------------------- */

/**
 * Convert a pointer event into normalized device coordinates.
 */
function setMouseFromEvent(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
}

/**
 * Click handler – performs ray‑casting and toggles selection.
 */
function onPointerDown(event) {
  // Only left‑button clicks
  if (event.button !== 0) return;

  setMouseFromEvent(event);
  raycaster.setFromCamera(mouse, camera);

  // intersect only demo meshes (skip helpers, lights, etc.)
  const intersectObjs = raycaster.intersectObjects(
    scene.children.filter(obj => obj.userData.isDemoMesh),
    false
  );

  if (intersectObjs.length > 0) {
    const mesh = intersectObjs[0].object;
    const itemId = mesh.userData.itemId;
    const item = Model.items.find(i => i.id === itemId);
    selectItem(item, mesh);
  } else {
    // Clicked empty space → clear selection
    clearSelection();
  }
}

/**
 * Highlight the selected mesh and store the reference to the selected Item.
 *
 * @param {Item} item - the data model instance
 * @param {THREE.Mesh} mesh - the mesh that was hit
 */
function selectItem(item, mesh) {
  clearSelection(); // remove old outline if any

  selectedItem = item;

  // Visual feedback – we use a BoxHelper (lightweight) that wraps the mesh.
  const box = new THREE.BoxHelper(mesh, CONFIG.selectOutlineColor);
  box.name = "__selectionOutline";
  box.userData.isSelectionOutline = true;
  scene.add(box);

  // OPTIONAL: if you already have a post‑processing OutlinePass you could
  // set `outlinePass.selectedObjects = [mesh];` instead.
}

/**
 * Remove any existing outline and clear the `selectedItem` variable.
 */
function clearSelection() {
  // Remove BoxHelper (if present)
  const outline = scene.getObjectByName("__selectionOutline");
  if (outline) scene.remove(outline);
  // Reset any post‑processing outline (if used)
  // if (outlinePass) outlinePass.selectedObjects = [];

  selectedItem = null;
}

/* -------------------------------------------------------------------------
   6️⃣  STATISTICS CALCULATOR -------------------------------------------------
   ------------------------------------------------------------------------- */

/**
 * Compute a snapshot of statistics for the current model.
 *
 * Returned object shape (example):
 * {
 *   totalItems: 27,
 *   averageDistanceFromOrigin: 8.3,
 *   mostCommonType: "cube",
 *   typeBreakdown: {cube: 12, sphere: 9, cone: 6}
 * }
 *
 * @returns {Object}
 */
function getStats() {
  const stats = {
    totalItems: Model.items.length
  };

  // Early‑exit for empty scenes (avoid division by zero)
  if (Model.items.length === 0) {
    return Object.assign(stats, {
      averageDistanceFromOrigin: 0,
      mostCommonType: null,
      typeBreakdown: {}
    });
  }

  // ---- Average distance from origin --------------------------------------
  if (CONFIG.stats.includeAverageDistance) {
    const sumDist = Model.items.reduce((sum, itm) => {
      return sum + itm.position.length(); // length() = distance from (0,0,0)
    }, 0);
    stats.averageDistanceFromOrigin = +(sumDist / Model.items.length).toFixed(2);
  }

  // ---- Most common type ---------------------------------------------------
  if (CONFIG.stats.includeMostCommonType || CONFIG.stats.includeTypesBreakdown) {
    const typeCounts = {};
    Model.items.forEach(itm => {
      typeCounts[itm.type] = (typeCounts[itm.type] || 0) + 1;
    });

    if (CONFIG.stats.includeTypesBreakdown) {
      stats.typeBreakdown = typeCounts;
    }

    if (CONFIG.stats.includeMostCommonType) {
      let most = null;
      let max = -Infinity;
      for (const [type, cnt] of Object.entries(typeCounts)) {
        if (cnt > max) {
          max = cnt;
          most = type;
        }
      }
      stats.mostCommonType = most;
    }
  }

  return stats;
}

/* -------------------------------------------------------------------------
   7️⃣  PUBLIC API ------------------------------------------------------------
   ------------------------------------------------------------------------- */

/**
 * Exported namespace – everything that other pages (`index.html`,
 * `stats.html`, or any module) might need.
 *
 * In a plain‑script scenario you can just expose the object on `window`.
 * If you bundle with ES‑modules, replace the last line with `export default API;`.
 */
const API = {
  // model helpers
  Item,
  Model,

  // persistence
  saveToStorage,
  loadFromStorage,
  resetStorage,

  // three‑js entry point
  initThree,

  // selection (useful for UI panels that want to display the selected item)
  getSelectedItem: () => selectedItem,

  // stats
  getStats,

  // convenience: add a brand‑new item at a given position
  createAndAddItem: ({ type = "cube", name = "New", position = new THREE.Vector3() } = {}) => {
    const item = new Item({ type, name, position });
    Model.items.push(item);
    addItemToScene(item).then(() => saveToStorage());
    return item;
  }
};

/* -------------------------------------------------------------------------
   8️⃣  AUTO‑INIT (optional) ---------------------------------------------------
   ------------------------------------------------------------------------- */

/**
 * If you embed this script directly with a `<script src="script.js"></script>`
 * tag and have a canvas with the id given in `CONFIG.canvasId`, you can
 * auto‑initialize everything by uncommenting the line below.
 *
 * If you prefer manual bootstrapping (e.g. you have multiple pages that
 * import this file as a module), just call `API.initThree()` from the
 * appropriate place.
 */
// API.initThree(); // <-- uncomment if you want auto‑start

/* -------------------------------------------------------------------------
   9️⃣  EXPOSE TO GLOBAL SCOPE -------------------------------------------------
   ------------------------------------------------------------------------- */

window.Demo3D = API; // make it globally available as `Demo3D`

/* =========================================================================
   END OF script.js
   ========================================================================= */
```

### How to use it

| Situation | What to do |
|----------|------------|
| **Simple page that just wants the scene** | Include the script, add `<canvas id="three-canvas"></canvas>` and call `Demo3D.initThree();` (or uncomment the auto‑init line). |
| **Create a new object from UI** | `Demo3D.createAndAddItem({ type: "sphere", name: "Ball", position: new THREE.Vector3(2,1,0) });` |
| **Read statistics from `stats.html`** | Load the script (or import the module) and call `Demo3D.getStats();`. The returned plain object can be JSON‑stringified or directly inserted into the DOM. |
| **Reset everything** | `Demo3D.resetStorage();` (clears `localStorage` and removes meshes). |
| **Select an object programmatically** | Find an `Item` (e.g. by id) and call `Demo3D.getSelectedItem()` after a click, or invoke `Demo3D.createAndAddItem` and then `Demo3D.selectItem(item, mesh)` – the latter is internal, but you can expose a wrapper if you need it. |

Feel free to adapt the `CONFIG.geometryMap` to load GLTF models, add textures, or change materials – the rest of the script works agnostic of the exact mesh you give it. Happy coding!