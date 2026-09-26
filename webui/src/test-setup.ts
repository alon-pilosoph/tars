// The unit tests run in Node; params.ts reads the page's link.
Object.assign(globalThis, { location: new URL("http://tars.test/") });
