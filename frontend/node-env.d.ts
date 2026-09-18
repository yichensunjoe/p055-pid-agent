// Minimal ambient declaration for the Node globals read by the build configs.
// Declared here instead of pulling in the whole @types/node package, which this
// frontend does not otherwise depend on.
declare const process: {
  env: Record<string, string | undefined>;
  cwd(): string;
};
