/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_MOCK_CAPTURE?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
