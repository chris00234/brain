
Prevents CPU from throttling down aggressively. Improves responsiveness during builds, compilation, and multitasking. Prioritizes performance over battery life.

## Enable Ultimate Performance

### Step 1: Create the plan (one-time)
```powershell
powercfg -duplicatescheme e9a42b02-d5df-448d-aa00-03f14749eb61
```

### Step 2: Activate it
```powershell
powercfg /setactive eb8b0891-1493-4344-b693-93901fbe5fc0
```

## Disable (Switch Back to Balanced)
```powershell
powercfg /setactive 381b4222-f694-41f0-9685-ff5bb260df2e
```

## Verify Active Plan
```powershell
powercfg /getactivescheme
```

## Notes
- The GUID `eb8b0891-1493-4344-b693-93901fbe5fc0` was generated when the plan was created on this machine — it may differ on other systems.
- Battery life will be shorter when Ultimate Performance is active on laptop power.
- The Balanced plan GUID (`381b4222-f694-41f0-9685-ff5bb260df2e`) is the same across all Windows installations.
