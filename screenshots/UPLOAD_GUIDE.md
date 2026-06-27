# Screenshot Upload Guide

## Required Screenshots

Upload these screenshots to this folder, then update the main README.md.

### 1. **slack-request-modal.png**
- Open Slack → Type `/access-request`
- Screenshot the modal form
- Show all fields: project, service, role, duration, ticket

### 2. **slack-approval-message.png**
- Screenshot a request message in #access-requests channel
- Show the approval buttons: [Approve] [Reject] [Comment]
- Include request details (who, what, why)

### 3. **admin-dashboard.png**
- Open http://localhost:8000/admin
- Screenshot the main dashboard
- Show stats, recent requests, quick actions

### 4. **rules-management.png**
- Navigate to Admin → Rules
- Screenshot the rules list page
- Show multiple rules with different priorities

### 5. **request-history.png**
- Navigate to Admin → Request History
- Screenshot the audit log page
- Show approved/rejected/pending requests

### 6. **approval-groups.png** (Optional)
- Navigate to Admin → Approver Groups
- Screenshot the groups management page
- Show group names and members

### 7. **argo-workflow.png** (Optional)
- Screenshot Argo Workflows UI showing a running workflow
- Show the workflow triggered after approval

---

## How to Use Screenshots in README

Once uploaded, add to README.md:

```markdown
## 📸 Screenshots

### Slack Request Modal
![Request Modal](screenshots/slack-request-modal.png)

### Approval Flow
![Approval Message](screenshots/slack-approval-message.png)

### Admin Dashboard
![Dashboard](screenshots/admin-dashboard.png)

### Rules Management
![Rules](screenshots/rules-management.png)
```

---

## Image Compression

Before uploading:
1. Resize to max 1920x1080: `sips -Z 1920 image.png` (Mac)
2. Compress: https://tinypng.com
3. Keep files under 300KB each

---

## Blur Sensitive Data

Use these tools to blur:
- **Mac**: Skitch (free)
- **Online**: https://www.photopea.com (free Photoshop)
- **CLI**: ImageMagick `convert input.png -blur 0x8 output.png`

---

## Placeholder Images (Until Real Screenshots)

If you don't have screenshots yet, you can:

1. Use mockups from https://mockuphone.com
2. Create diagrams with https://excalidraw.com
3. Use placeholder: `![Coming Soon](https://via.placeholder.com/800x400?text=Screenshot+Coming+Soon)`

---

## After Uploading

1. Add screenshots to this folder
2. Update main README.md with image links
3. Commit: `git add screenshots/ && git commit -m "Add product screenshots"`
4. Push: `git push`

Your repo will instantly look more professional! 🚀
