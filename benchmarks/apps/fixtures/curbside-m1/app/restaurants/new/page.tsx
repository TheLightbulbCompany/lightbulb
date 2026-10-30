import { RestaurantForm } from "@/components/restaurant-form";
import { requireUser } from "@/lib/auth";

export default async function NewRestaurant() {
  await requireUser();
  return <RestaurantForm />;
}
