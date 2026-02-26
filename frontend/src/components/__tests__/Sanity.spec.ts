/**
 * frontend/src/components/__tests__/Sanity.spec.ts
 *
 * Purpose:
 * Basic Vitest sanity check to validate the Vue unit-test runner setup.
 */

import { defineComponent } from "vue";
import { mount } from "@vue/test-utils";
import { describe, it, expect } from "vitest";

describe("Frontend test runner sanity", () => {
  it("mounts a simple Vue component", () => {
    const Demo = defineComponent({
      template: "<div data-testid='demo'>ok</div>",
    });

    const wrapper = mount(Demo);
    expect(wrapper.get("[data-testid='demo']").text()).toBe("ok");
  });
});
