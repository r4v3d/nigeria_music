import random
import time

def generate_human_track(distance):
    """
    Generates a list of (dx, dy, sleep_time) steps to move a total of `distance` pixels.
    Uses a physical spring-damper model (PD controller) with noise and optional
    overshoot/correction to simulate human hand movement.
    
    Args:
        distance (int): The total distance to slide the button (in pixels).
        
    Returns:
        list of tuples: [(dx, dy, sleep_time), ...] where:
            - dx (int): Relative horizontal movement for this step.
            - dy (int): Relative vertical movement for this step.
            - sleep_time (float): Time to sleep after this step (in seconds).
    """
    if distance <= 0:
        return []

    track = []
    
    current_x = 0.0
    current_y = 0.0
    v_x = 0.0
    v_y = 0.0
    
    target_x = float(distance)
    target_y = 0.0
    
    # Physics constants for PD controller (spring-damper)
    kp = 3.0   # Proportional gain (spring stiffness)
    kd = 0.8   # Damping gain (friction)
    dt = 0.04  # Time step size per iteration (in seconds)
    
    # Simulate human imprecision: 60% chance to overshoot by 2 to 7 pixels
    overshoot_offset = random.randint(2, 7) if random.random() < 0.6 else 0
    temp_target_x = target_x + overshoot_offset
    
    has_corrected = False
    max_steps = 200
    step = 0
    
    while step < max_steps:
        step += 1
        
        # If we overshot and reached/passed the target, correct back to the real target
        if overshoot_offset > 0 and not has_corrected and current_x >= target_x:
            temp_target_x = target_x
            has_corrected = True
            v_x *= 0.4  # Human pauses/decelerates sharply before correcting back
            
        dx = temp_target_x - current_x
        dy = target_y - current_y
        
        # Add random noise to the acceleration
        # Noise decreases as we get closer to the final destination to simulate precision adjustment
        dist_factor = max(0.1, min(1.0, abs(dx) / distance))
        noise_x = random.uniform(-2.0, 2.0) * dist_factor
        noise_y = random.uniform(-0.8, 0.8) * dist_factor
        
        # Calculate accelerations (PD controller formula: a = Kp*error - Kd*velocity + noise)
        ax = kp * dx - kd * v_x + noise_x
        ay = kp * dy - kd * v_y + noise_y
        
        # Update velocities
        v_x += ax * dt
        v_y += ay * dt
        
        # Calculate proposed displacements
        move_x = v_x * dt
        move_y = v_y * dt
        
        # Add micro-jitters to displacement occasionally
        if random.random() < 0.15:
            move_y += random.choice([-1.0, 1.0])
            
        current_x += move_x
        current_y += move_y
        
        # Round displacements to integer pixels
        rx = round(move_x)
        ry = round(move_y)
        
        # Delay simulating physical reaction time/mouse updates (typically 8ms to 22ms)
        step_sleep = random.uniform(0.008, 0.022)
        
        if rx != 0 or ry != 0:
            track.append((rx, ry, step_sleep))
            
        # Stopping condition: close to target and correction is complete
        if abs(current_x - target_x) < 0.7 and (overshoot_offset == 0 or has_corrected):
            break
            
    # Guarantee exact target displacement by appending correction if needed
    total_x_moved = sum(t[0] for t in track)
    diff = distance - total_x_moved
    if diff != 0:
        track.append((diff, 0, random.uniform(0.01, 0.03)))
        
    return track


def solve_with_selenium(driver, slider_element, distance):
    """
    Solves the slider captcha using Selenium webdriver.
    
    Args:
        driver: Selenium WebDriver instance.
        slider_element: The web element representing the draggable slider handle.
        distance (int): The slider track length to move.
    """
    from selenium.webdriver.common.action_chains import ActionChains
    
    # 1. Generate the human track
    track = generate_human_track(distance)
    
    # 2. Instantiate Actions
    actions = ActionChains(driver)
    
    # 3. Click and hold the slider
    actions.click_and_hold(slider_element).perform()
    time.sleep(random.uniform(0.15, 0.3))  # Human reaction delay after clicking
    
    # 4. Perform the steps sequentially
    for dx, dy, sleep_time in track:
        # Move by offset relative to current mouse position
        actions.move_by_offset(dx, dy).perform()
        time.sleep(sleep_time)
        
    # 5. Release the mouse
    time.sleep(random.uniform(0.1, 0.2))  # Pause before letting go
    actions.release().perform()


def solve_with_playwright(page, slider_selector, distance):
    """
    Solves the slider captcha using Playwright.
    
    Args:
        page: Playwright page object.
        slider_selector (str): CSS selector for the slider handle element.
        distance (int): The distance to drag.
    """
    # Find the bounding box of the slider element
    slider = page.locator(slider_selector)
    box = slider.bounding_box()
    if not box:
        raise ValueError(f"Could not find bounding box for selector: {slider_selector}")
        
    # Start coordinates (center of the slider handle)
    start_x = box['x'] + box['width'] / 2
    start_y = box['y'] + box['height'] / 2
    
    # 1. Generate the human track
    track = generate_human_track(distance)
    
    # 2. Hover over the slider and press mouse down
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    time.sleep(random.uniform(0.15, 0.3))
    
    # 3. Move intermediate steps
    current_x = start_x
    current_y = start_y
    for dx, dy, sleep_time in track:
        current_x += dx
        current_y += dy
        page.mouse.move(current_x, current_y)
        time.sleep(sleep_time)
        
    # 4. Release mouse
    time.sleep(random.uniform(0.1, 0.2))
    page.mouse.up()


def solve_with_pyautogui(start_x, start_y, distance):
    """
    Solves the slider captcha by physically controlling the system mouse cursor via PyAutoGUI.
    Useful for desktop testing or manual browser automation checks.
    
    Args:
        start_x (int): X coordinate where the slider handle is on the screen.
        start_y (int): Y coordinate where the slider handle is on the screen.
        distance (int): Distance to drag.
    """
    import pyautogui
    
    # Generate human track
    track = generate_human_track(distance)
    
    # Move to start and press down
    pyautogui.moveTo(start_x, start_y, duration=random.uniform(0.1, 0.2))
    pyautogui.mouseDown()
    time.sleep(random.uniform(0.15, 0.25))
    
    # Drag along the track
    current_x = start_x
    current_y = start_y
    for dx, dy, sleep_time in track:
        current_x += dx
        current_y += dy
        pyautogui.moveTo(current_x, current_y)
        time.sleep(sleep_time)
        
    # Release
    time.sleep(random.uniform(0.1, 0.18))
    pyautogui.mouseUp()
