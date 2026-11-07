import { fireEvent, render, screen } from "@testing-library/react-native";

import { RoomHeaderActions } from "../RoomHeaderActions";

describe("RoomHeaderActions", () => {
  it("labels the pair Star/Mute this conversation, unselected, on a plain room", async () => {
    await render(
      <RoomHeaderActions
        starred={false}
        muted={false}
        onToggleStar={() => {}}
        onToggleMute={() => {}}
      />,
    );
    const star = screen.getByLabelText("Star");
    const mute = screen.getByLabelText("Mute");
    expect(star.props.accessibilityState.selected).toBe(false);
    expect(mute.props.accessibilityState.selected).toBe(false);
  });

  it("flips to Unstar/Unmute this conversation, selected, on a starred and muted room", async () => {
    await render(
      <RoomHeaderActions
        starred={true}
        muted={true}
        onToggleStar={() => {}}
        onToggleMute={() => {}}
      />,
    );
    const star = screen.getByLabelText("Unstar");
    const mute = screen.getByLabelText("Unmute");
    expect(star.props.accessibilityState.selected).toBe(true);
    expect(mute.props.accessibilityState.selected).toBe(true);
  });

  it("calls through on press", async () => {
    const onToggleStar = jest.fn();
    const onToggleMute = jest.fn();
    await render(
      <RoomHeaderActions
        starred={false}
        muted={false}
        onToggleStar={onToggleStar}
        onToggleMute={onToggleMute}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Star"));
    await fireEvent.press(screen.getByLabelText("Mute"));
    expect(onToggleStar).toHaveBeenCalledTimes(1);
    expect(onToggleMute).toHaveBeenCalledTimes(1);
  });

  it("disables both buttons and reports it to accessibility", async () => {
    const onToggleStar = jest.fn();
    await render(
      <RoomHeaderActions
        starred={false}
        muted={false}
        disabled
        onToggleStar={onToggleStar}
        onToggleMute={() => {}}
      />,
    );
    const star = screen.getByLabelText("Star");
    expect(star.props.accessibilityState.disabled).toBe(true);
    await fireEvent.press(star);
    expect(onToggleStar).not.toHaveBeenCalled();
  });

  it("puts the call button where the star was when the room can go Live", async () => {
    const onPress = jest.fn();
    await render(
      <RoomHeaderActions starred={false} muted={false} onToggleStar={jest.fn()} onToggleMute={jest.fn()} voice={{ label: "Go live", onPress }} />,
    );
    expect(screen.queryByLabelText("Star")).toBeNull();
    await fireEvent.press(screen.getByLabelText("Go live"));
    expect(onPress).toHaveBeenCalled();
  });

  it("shows the speaking agent's avatar and says who is speaking", async () => {
    await render(
      <RoomHeaderActions starred={false} muted={false} onToggleStar={jest.fn()} onToggleMute={jest.fn()}
        voice={{ active: true, speaking: { name: "Gauge" }, label: "Open Live, Gauge speaking", onPress: jest.fn() }} />,
    );
    expect(screen.getByLabelText("Open Live, Gauge speaking")).toBeTruthy();
    expect(screen.getByText("G")).toBeTruthy();
  });
});
