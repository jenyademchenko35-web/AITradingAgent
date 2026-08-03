// @vitest-environment node

import { expect, test } from "vitest";
import type { UserConfig } from "vite";
import viteConfig from "../vite.config";


test("proxies only development API requests to the local backend", () => {
  const config = viteConfig as UserConfig;
  expect(config.server?.proxy?.["/api"]).toEqual({
    target: "http://127.0.0.1:8081",
  });
  expect(config.base).toBeUndefined();
});
