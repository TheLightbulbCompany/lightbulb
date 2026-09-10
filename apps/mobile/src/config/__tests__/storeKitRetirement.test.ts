import eas from "../../../eas.json";

it("does not embed a StoreKit subscription SKU in any mobile build profile", () => {
  for (const profile of Object.values(eas.build)) {
    const env = "env" in profile ? profile.env : {};
    expect(env).not.toHaveProperty("EXPO_PUBLIC_APPLE_IAP_PRODUCT_ID");
  }
});
