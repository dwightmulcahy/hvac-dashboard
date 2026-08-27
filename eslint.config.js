// ESLint config — deliberately scoped to tests-js/**/*.js only.
//
// hvac-dashboard.html and kiosk.html both have their JS embedded
// directly in a single <script> tag (no build step, by design — see
// docs/ARCHITECTURE.md), not as standalone .js files ESLint can lint
// directly. Linting embedded script requires either extracting it
// first (tests-js/extract.js already does this, but only for pulling
// out sentinel-marked pure functions for testing, not the whole file)
// or an HTML-aware ESLint plugin — either path is a bigger, separate
// piece of work than "add basic lint tooling," and risks surfacing
// findings across ~4,000 lines of already-shipped, heavily-tested
// production code with no way to verify each one is a real issue
// rather than a false positive from a linter that's never seen this
// codebase before. Scoping to the small, low-risk tests-js/ files
// first is the safer place to actually turn this on.
module.exports = [
  {
    files: ["tests-js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "commonjs",
      globals: {
        require: "readonly",
        module: "readonly",
        __dirname: "readonly",
        process: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
      },
    },
    rules: {
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
      "no-undef": "error",
      "no-var": "error",
      "prefer-const": "warn",
      eqeqeq: ["warn", "smart"],
    },
  },
];
