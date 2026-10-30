// Ambient module declarations for non-code imports.
//
// Some packages ship a plain stylesheet we import for its side effects
// (e.g. `import "@xterm/xterm/css/xterm.css"` — required for correct glyph
// metrics in the device terminal). `tsc` has no type for a `.css` module, so
// declare the wildcard here. Next/webpack handles the actual bundling.
declare module "*.css";
