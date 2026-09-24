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
    const star = screen.getByLabelText("Star this conversation");
    const mute = screen.getByLabelText("Mute this conversation");
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
    const star = screen.getByLabelText("Unstar this conversation");
    const mute = screen.getByLabelText("Unmute this conversation");
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
    await fireEvent.press(screen.getByLabelText("Star this conversation"));
    await fireEvent.press(screen.getByLabelText("Mute this conversation"));
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
    const star = screen.getByLabelText("Star this conversation");
    expect(star.props.accessibilityState.disabled).toBe(true);
    await fireEvent.press(star);
    expect(onToggleStar).not.toHaveBeenCalled();
  });

  it("offers Leave only when the screen passes a handler", async () => {
    const onLeave = jest.fn();
    const { rerender } = await render(
      <RoomHeaderActions starred={false} muted={false} onToggleStar={() => {}} onToggleMute={() => {}} />,
    );
    expect(screen.queryByLabelText("Leave this conversation")).toBeNull();
    await rerender(
      <RoomHeaderActions
        starred={false}
        muted={false}
        onToggleStar={() => {}}
        onToggleMute={() => {}}
        onLeave={onLeave}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Leave this conversation"));
    expect(onLeave).toHaveBeenCalledTimes(1);
  });
});
