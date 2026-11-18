package handler

import (
	"net/http"
	"ragflow/internal/common"
	"ragflow/internal/dao"
	"ragflow/internal/entity"

	"github.com/gin-gonic/gin"
)

type AccessLevelHandler struct {
	userDAO     *dao.UserDAO
	documentDAO *dao.DocumentDAO
}

func NewAccessLevelHandler() *AccessLevelHandler {
	return &AccessLevelHandler{
		userDAO:     dao.NewUserDAO(),
		documentDAO: dao.NewDocumentDAO(),
	}
}

type accessLevelRequest struct {
	AccessLevel *int `json:"access_level" binding:"required,min=0,max=3"`
}

func (h *AccessLevelHandler) SetUserAccessLevel(c *gin.Context) {
	if _, ok := requireAccessLevelAdministrator(c); !ok {
		return
	}
	var req accessLevelRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		common.ResponseWithCodeData(c, common.CodeArgumentError, nil, err.Error())
		return
	}
	userID := c.Param("user_id")
	if err := h.userDAO.UpdateAccessLevel(c.Request.Context(), dao.DB, userID, *req.AccessLevel); err != nil {
		common.ErrorWithCode(c, common.CodeDataError, err.Error())
		return
	}
	common.SuccessWithData(c, gin.H{"user_id": userID, "access_level": *req.AccessLevel}, "success")
}

func (h *AccessLevelHandler) SetDocumentAccessLevel(c *gin.Context) {
	if _, ok := requireAccessLevelAdministrator(c); !ok {
		return
	}
	var req accessLevelRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		common.ResponseWithCodeData(c, common.CodeArgumentError, nil, err.Error())
		return
	}
	documentID := c.Param("document_id")
	if _, err := h.documentDAO.GetByID(c.Request.Context(), dao.DB, documentID); err != nil {
		common.ErrorWithCode(c, common.CodeNotFound, "document not found")
		return
	}
	if err := h.documentDAO.UpdateByID(c.Request.Context(), dao.DB, documentID, map[string]interface{}{"access_level": *req.AccessLevel}); err != nil {
		common.ErrorWithCode(c, common.CodeDataError, err.Error())
		return
	}
	common.SuccessWithData(c, gin.H{"document_id": documentID, "access_level": *req.AccessLevel}, "success")
}

func requireAccessLevelAdministrator(c *gin.Context) (*entity.User, bool) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		common.ErrorWithCode(c, code, message)
		return nil, false
	}
	if user.IsSuperuser == nil || !*user.IsSuperuser {
		common.ResponseWithHttpCodeData(c, http.StatusForbidden, common.CodeForbidden, nil, "Only an administrator can change access levels")
		return nil, false
	}
	return user, true
}